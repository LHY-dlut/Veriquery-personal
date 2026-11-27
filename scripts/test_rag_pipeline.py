#!/usr/bin/env python3
"""Regression tests for evidence retrieval and HTTP responses without model servers."""
import asyncio
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.dependencies import ServiceContainer, get_service_container
from api.routers.chat import _deduplicate_sentences, router
from agents.workflow_nodes import TextAnalysisNodes
from api.routers.circuit import _resolve_image_path
from api.routers.documents import _build_chunks_from_pages
from core.config import Settings
from core.llm_client import LLMClient
from core.sqlite_utils import SQLiteCorruptedError
from ingestion.document_processor import EnhancedDocumentProcessor
from ingestion.mineru_client import MinerUClient, _parse_table_content
from ingestion.spec_column_evidence import annotate_spec_columns
from retrieval.bm25_store import BM25Store
from retrieval.hybrid_retriever import HybridRetriever
from retrieval.dac_calculation import calculate_dac
from retrieval.verified_passages import find_condition_excerpt, find_verified_excerpt
from retrieval.table_evidence import TableEvidenceQuery
from retrieval.table_store import TableStore


def quote(chunk_id, text, score, *, page=5, doc="ina", source="pdf"):
    return SimpleNamespace(
        id=chunk_id, text=text, score=score,
        metadata={"document_id": doc, "filename": f"{doc}.pdf", "page": page, "source": source},
    )


class VectorSearch:
    SUPPORTED_FILTER_KEYS = ("document_id", "document_ids", "page")

    def __init__(self, records):
        self.records = records

    async def search(self, *, query_embedding, top_k, filter_dict):
        ids = (filter_dict or {}).get("document_ids")
        page = (filter_dict or {}).get("page")
        return [
            row for row in self.records
            if (not ids or row.metadata["document_id"] in ids)
            and (page is None or row.metadata["page"] == page)
        ][:top_k]


class TestRagPipeline(unittest.TestCase):
    def test_dac_math_uses_supplied_code_units_and_source_definitions(self):
        directory = Path(__file__).resolve().parents[1] / "data/eval/nine-stage/results"
        case = json.loads((directory / "n04-chain.json").read_text(encoding="utf8"))
        evidence = case["evidence"]
        doc_ids = {item["document_id"] for item in evidence}
        def calculate(query):
            return calculate_dac(query, evidence, case["context"], doc_ids)

        query = "MCP4725 VDD=2048 mV，输入码为 0x40，实测 VOUT=31.75 mV，理想输出和有符号 INL 是多少？"
        answer, refs = calculate(query)
        self.assertIn("32 mV", answer)
        self.assertIn("-0.5 LSB", answer)
        self.assertGreaterEqual(len(refs), 2)
        self.assertIn("无法表示", calculate(query.replace("0x40", "0x1000"))[0])
        self.assertIsNone(calculate(query.replace("MCP4725", "ABC1234")))
        self.assertIsNone(calculate(query.replace("VDD=", "VDD=5 V 或 VDD=")))
        self.assertIsNone(calculate_dac(query, evidence, "", doc_ids))

    def test_family_member_binding_is_local_and_ambiguity_defers(self):
        row = "VOUT=VA*(D/2^n)\nresolution in bits: 7 (AB123), 11 (AB456)"
        def calculate(text, query):
            item = {"text_snippet": text, "file": "series.pdf", "document_id": "doc"}
            return calculate_dac(query, [item], text, {"doc"})

        question = "AB456 ideal output voltage for code 0x200?"
        self.assertIn("1/4 × VA", calculate(row, question)[0])
        self.assertIsNone(calculate(row.replace("11 (AB456)", "11 (AB456), 9 (AB456)"), question))
        self.assertIsNone(calculate(row, question.replace("AB456", "AB457")))
        self.assertIsNone(calculate(row.replace("D/2^n)", "D/2^n)+1"), question))
        self.assertIsNone(calculate(row, question + " Compare to 0x100."))
        self.assertIsNone(calculate(row, "AB456 code 0x200 is loaded. What is the supply current?"))
        self.assertIn("0–2047", calculate(row, "AB456: is data code 0x800 valid?")[0])

    def test_chat_citation_contract_preserves_same_page_identity_and_legacy_records(self):
        from api.routers.chat import _extract_results_from_state
        rows = [{"document_id": "a", "chunk_id": k, "page": 2, "file": "part.pdf", "text_snippet": k}
                for k in ("formula", "conditions")]
        result = _extract_results_from_state({"citations": rows + [{"file":"old.pdf", "page":1}]})
        self.assertEqual([c.chunk_id for c in result.citations], ["formula", "conditions", None])
        self.assertEqual([c.document_id for c in result.citations], ["a", "a", None])

    def test_feedback_row_requires_matching_device_mode_and_labelled_values(self):
        from retrieval.dac_calculation import _feedback_row
        header = "PARAMETER | TEST CONDITIONS | MAX | MIN | UNIT"
        source = "ABC123 feedback voltage | A/B = V_IN | 520 | 480 | mV"
        def find(row, mode="VIN"):
            return _feedback_row("feedback voltage at A/B=" + mode,
                                 [(4, {"text_snippet": header + "\n" + row})], "ABC123")

        self.assertEqual(find(source)[:4], (480, 520, "mV", 4))
        self.assertIsNone(find(source, "GND"))
        self.assertIsNone(find(source.replace("ABC123", "DEF456")))
        self.assertIsNone(find(source.replace("520 | 480", "480 | 520")))

    def test_long_answer_retains_both_frequency_conclusions(self):
        path = Path(__file__).resolve().parents[1] / "data/eval/formula-ingest-ab/results/budget-check.jsonl"
        rows = map(json.loads, path.read_text(encoding="utf8").splitlines())
        record = next(row for row in rows if row["id"] == "n06" and row["limit"] == 768)
        cleaned = LLMClient(self.settings)._clean_answer(record["raw"])
        for answer in (cleaned, _deduplicate_sentences(cleaned)[0]):
            self.assertIn("满量程范围是 0.4 MHz", answer)
            self.assertIn("最大允许偏差是 200 kHz", answer)
            self.assertIn("[1]", answer)

    def test_cross_document_context_retains_formula_definitions_and_labelled_row(self):
        directory = Path(__file__).resolve().parents[1] / "data/eval/nine-stage/results"
        case = json.loads((directory / "n10-chain.json").read_text(encoding="utf8"))
        with gzip.open(directory / "events.jsonl.gz", "rt", encoding="utf8") as stream:
            event = next(row for row in map(json.loads, stream)
                         if row["session"] == case["session"] and row["stage"] == "graph_output")
        candidates = [SimpleNamespace(chunk_id=row["id"], document_id=row["doc"], page=row["page"],
                                      text=row["text"], metadata=row["metadata"],
                                      filename=row["metadata"].get("filename", ""))
                      for row in event["results"]]
        context, evidence = TextAnalysisNodes(self.settings)._build_evidence_context(
            case["entry"]["query"], candidates, comparison=True, joint=True)
        self.assertLessEqual(len(context), 4200)
        formula = next(row for row in candidates if row.chunk_id == "c06748dc_p2_c8")
        feedback = next(row for row in candidates if row.chunk_id == "386a2473_p1_tbl7")
        self.assertIn(formula.text, context)
        self.assertIn("MIN | TYP | MAX | UNIT\n" + feedback.text, context)
        self.assertTrue({formula.chunk_id, feedback.chunk_id} <= {item["chunk_id"] for item in evidence})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(DATA_DIR_STR=self.temp.name)
        self.container = ServiceContainer()
        self.container.settings = self.settings
        self.container._table_store = TableStore(str(Path(self.temp.name) / "tables.db"))

    def client(self, rows, answer):
        # Only the vector backend and external model responses are substituted.
        # Use the same graph constructor, SQLite, BM25, RRF, prompt and API as chat.
        embedder = SimpleNamespace(embed_query=lambda query: [1.0])
        self.container._retriever = HybridRetriever(
            settings=self.settings,
            embedding_manager=embedder,
            vector_store=VectorSearch(rows),
            bm25_store=self.container.bm25_store,
            table_store=self.container.table_store,
        )
        graph = self.container.veriquery_graph
        llm = LLMClient(self.settings)
        llm._generate = Mock(return_value=answer)
        graph.agent_nodes.text_nodes._llm_client = llm
        self.assertIs(graph.agent_nodes.retriever.bm25_store, self.container.bm25_store)

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_service_container] = lambda: self.container
        self.llm = llm
        return TestClient(app)

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_chat_keeps_shunt_and_bus_evidence_and_returns_cited_row(self, rerank_post):
        rows = [
            quote("fake", "Shunt voltage | 1.25 | mV", 1, source="param_record"),
            quote("shunt", "Shunt voltage | 2.5 | µV", .9, source="table_text"),
            quote("bus", "Bus voltage | 1.25 | mV", .8, source="table_text"),
            quote("other", "Shunt voltage | 10 | µV", .7, doc="other"),
        ]
        rerank_post.return_value.json.return_value = {
            "results": [{"index": 0, "relevance_score": .1}, {"index": 1, "relevance_score": .9}]
        }
        client = self.client(rows, "Shunt voltage: 2.5 µV per LSB [2]")
        data = {"query": "What is the INA226 shunt voltage per LSB?", "document_ids": ["ina"]}
        response = client.post("/api/v1/chat/", json=data).json()
        self.assertTrue(response["success"], response)
        self.assertIn("2.5", response["response"])
        self.assertIn("[1]", response["response"])
        self.assertEqual([c["text_snippet"] for c in response["citations"]], [rows[1].text])
        self.assertIsNone(response["trace"])
        self.container.settings.EVAL_TRACE_ENABLED = True
        traced = client.post("/api/v1/chat/", json={**data, "trace": True}).json()
        self.assertEqual([row["chunk_id"] for row in traced["trace"]["retrieved"]], ["bus", "shunt"])
        self.assertIn("[2] ina.pdf", traced["trace"]["context"])
        self.assertIn("2.5", traced["trace"]["context_evidence"][1]["text_snippet"])
        self.assertEqual(traced["trace"]["context_evidence"][1]["document_id"], "ina")
        rerank_payload = rerank_post.call_args.kwargs["json"]
        self.assertEqual(rerank_payload["query"], data["query"])
        self.assertEqual(rerank_payload["documents"], [rows[1].text, rows[2].text])
        for output in ("2.5 µV", "2.5 µV [9]", "文档中未找到相关内容 [1]"):
            self.llm._generate.return_value = output
            response = client.post("/api/v1/chat/", json=data).json()
            self.assertEqual(response["citations"], [])
            self.assertIn("没有足够的证据", response["response"])
        self.llm._generate.return_value = "Shunt voltage: 2.5 µV per LSB [2]"
        stream = client.post("/api/v1/chat/stream", json={**data, "trace": True})
        complete = [json.loads(line) for line in stream.text.splitlines() if line.strip()][-1]
        self.assertEqual(complete["type"], "complete")
        self.assertEqual(complete["data"]["citations"][0]["text_snippet"], rows[1].text)
        self.assertEqual(complete["data"]["trace"]["context_evidence"][1]["document_id"], "ina")

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_rank_42_register_row_can_reach_model_context(self, rerank_post):
        rows = [
            quote(str(i), f"Temperature accuracy candidate {i}", 1 - i / 100, doc="tmp")
            for i in range(41)
        ]
        register = quote("f380", "Temperature register | -25 °C | F380", .58, doc="tmp", page=13)
        rows.append(register)
        rerank_post.return_value.json.return_value = {
            "results": [{"index": 41, "relevance_score": 1.0}]
        }
        client = self.client(rows, "The register is F380 [1]")
        response = client.post("/api/v1/chat/", json={
            "query": "What is the TMP117 register value at -25 °C?", "document_ids": ["tmp"],
        }).json()
        self.assertEqual(len(rerank_post.call_args.kwargs["json"]["documents"]), 42)
        self.assertIn(register.text, self.llm._generate.call_args.args[0])
        self.assertEqual(response["citations"][0]["page"], 13)

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_hex_row_is_selected_with_its_header_and_original_citation(self, rerank_post):
        for document, code in [("tmp", "F380"), ("other", "1111")]:
            self.container.table_store.add({
                "document_id": document, "filename": f"{document}.pdf", "page": 13,
                "data": [
                    ["TEMPERATURE(°C)", "TEMPERATURE REGISTER VALUE", ""],
                    ["TEMPERATURE(°C)", "BINARY", "HEX"],
                    ["-25", "1111 0011 1000 0000", code],
                    ["25", "0000 1100 1000 0000", "0C80"],
                ],
            })
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        # A legacy vector row without a header can win RRF deduplication.
        client = self.client([quote("legacy", "-25 | 1111 0011 1000 0000 | F380", .9,
                                    doc="tmp", page=13, source="table_text")], "F380 [1]")
        data = {"query": "What is the TMP117 temperature register hexadecimal value at -25 °C?",
                "document_ids": ["tmp"], "trace": True}
        response = client.post("/api/v1/chat/", json=data).json()
        trace = response["trace"]
        self.assertTrue(trace["retrieved"][0]["focused_row"])
        self.assertIn("HEX: F380", trace["context"])
        self.assertNotIn("0C80", trace["context"])
        self.assertNotIn("1111 0011", trace["context"])
        self.assertIn("-25 | 1111 0011 1000 0000 | F380", response["citations"][0]["text_snippet"])
        self.assertEqual(trace["context_evidence"][0]["document_id"], "tmp")

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_exact_operating_row_displaces_conflicting_power_down_excerpt(self, rerank_post):
        rows = [
            quote("body", "Supply current Power-down 0.5 µA. Operating 150 µA.", 1, doc="ads"),
            quote("op", "IVDD | Supply current | Operating | TA = 25°C | PDF verified: TYP = 150; MAX = 200 | µA", .8, doc="ads", source="table_text"),
            quote("down", "IVDD | Supply current | Power-down | TA = 25°C | PDF verified: TYP = 0.5; MAX = 2 | µA", .7, doc="ads", source="table_text"),
        ]
        rerank_post.return_value.json.return_value = {"results": [
            {"index": 0, "relevance_score": .1}, {"index": 1, "relevance_score": .9},
            {"index": 2, "relevance_score": .8},
        ]}
        self.settings.EVAL_TRACE_ENABLED = True
        client = self.client(rows, "150 µA [1]\n[150]")
        response = client.post("/api/v1/chat/", json={
            "query": "What is the ADS1115 typical operating supply current at TA = 25 °C?",
            "document_ids": ["ads"], "trace": True,
        }).json()
        trace = response["trace"]
        self.assertEqual(trace["retrieved"][0]["chunk_id"], "op")
        self.assertGreater(trace["retrieved"][0]["rerank_position"], 1)
        self.assertNotIn("0.5", trace["context"])
        self.assertIn("TYP: 150", trace["context"])
        self.assertNotIn("[150]", response["response"])
        self.assertIn("MAX = 200", response["citations"][0]["text_snippet"])

    def test_advanced_latex_temperature_and_chinese_question_match_same_row(self):
        focus = TableEvidenceQuery.from_text("ADS1115 在 TA=25℃时的典型工作电流是多少？")
        row = "$I_{VDD}$ | Supply current | Operating | $T_A=25°C$ |  | 150 | 200 | μA"
        header = "PARAMETER |  | TEST CONDITIONS |  | MIN | TYP | MAX | UNIT"
        self.assertTrue(focus.matches(row, header))
        self.assertFalse(focus.matches(row.replace("Operating", "Power-down"), header))
        self.assertIn("TYP: 150", focus.model_text(row, header))
        self.assertNotIn("200", focus.model_text(row, header))

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_comparison_asks_each_device_and_remaps_its_citations(self, rerank_post):
        self.settings.RAG_JOINT_COMPARISON = False
        rerank_post.return_value.json.return_value = {"results": []}
        rows = [
            quote("p1", "OPA211 operates from 4.5 V to 36 V.", .9, doc="opa211"),
            quote("p2", "OPA188 operates from 4 V to 36 V.", .2, doc="opa188"),
        ]
        self.settings.EVAL_TRACE_ENABLED = True
        client = self.client(rows, "")
        self.llm._generate.side_effect = [
            "OPA211 maximum supply: 36 V",
            "OPA211 maximum supply: 36 V [1].",
            "OPA188 maximum supply: 36 V [1].",
        ]
        response = client.post("/api/v1/chat/", json={
            "query": "Compare the maximum supply voltage of the OPA211 and OPA188.",
            "document_ids": ["opa211", "opa188"], "trace": True,
        }).json()
        trace = response["trace"]
        self.assertIn("operating supply voltage range", trace["comparison_query"])
        self.assertEqual([r["document_id"] for r in trace["context_evidence"]], ["opa211", "opa188"])
        self.assertIn("OPA211 maximum supply: 36 V [1]", trace["comparison_calls"][0]["citation_repair"])
        self.assertIn("OPA188 maximum supply: 36 V [2]", response["response"])
        self.assertEqual([r["file"] for r in response["citations"]], ["opa211.pdf", "opa188.pdf"])

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_bare_literal_can_only_cite_exact_named_supply_range_column(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        self.container.table_store.add({
            "document_id": "family", "filename": "eeprom.pdf", "page": 1,
            "data": [["Part Number", "Vcc Range"], ["24AA256", "1.7V-5.5V"], ["24LC256", "2.5V-5.5V"]],
        })
        client = self.client([], "2.5V-5.5V")
        data = {"query": "What supply voltage range does the 24LC256 operate over?",
                "document_ids": ["family"], "trace": True}
        response = client.post("/api/v1/chat/", json=data).json()
        self.assertTrue(response["trace"]["citation_literal_verified"])
        self.assertIn("2.5V-5.5V [1]", response["response"])
        self.assertIn("24LC256 | 2.5V-5.5V", response["citations"][0]["text_snippet"])
        self.llm._generate.side_effect = [
            "The 24LC256 operates over a supply voltage range of 2.5V to 5.5V", "[1]"
        ]
        response = client.post("/api/v1/chat/", json=data).json()
        self.assertTrue(response["trace"]["citation_literal_verified"])
        self.llm._generate.side_effect = None
        self.llm._generate.return_value = "1.7V-5.5V"
        response = client.post("/api/v1/chat/", json=data).json()
        self.assertEqual(response["citations"], [])

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_comparison_can_quote_supported_ranges_when_model_misreads_them(self, rerank_post):
        self.settings.RAG_JOINT_COMPARISON = False
        rerank_post.return_value.json.return_value = {"results": []}
        rows = [
            quote("p1", "Wide supply range: 4.5 V to 36 V Rail-to-rail output.", .9, doc="opa211"),
            quote("p2", "Either single or dual supplies can be used in the range from 4 V to 36 V.", .8, doc="opa188"),
        ]
        self.settings.EVAL_TRACE_ENABLED = True
        client = self.client(rows, "-4.5 V to 36 V [1].")
        response = client.post("/api/v1/chat/", json={
            "query": "Compare the maximum supply voltage of OPA211 and OPA188.",
            "document_ids": ["opa211", "opa188"], "trace": True,
        }).json()
        self.assertIn("4.5 V to 36 V [1]", response["response"])
        self.assertIn("4 V to 36 V [2]", response["response"])
        self.assertNotIn("18 V", response["response"])
        self.assertNotIn("-4.5", response["response"])
        self.assertTrue(all(call["comparison_quote"] for call in response["trace"]["comparison_calls"]))

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_joint_comparison_supplies_both_sources_in_one_call(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        client = self.client([
            quote("left", "Power supply range from 2.8 V to 5.5 V.", 1, doc="lm75b"),
            quote("right", "2.7 V to 3.3 V supply voltage range.", 1, doc="max30205"),
        ], "5 V works for LM75B [1] but exceeds the MAX30205 limit [2].")
        body = {"query": "Compare the supply ranges of LM75B and MAX30205. Which supports 5 V?",
                "document_ids": ["lm75b", "max30205"], "trace": True}
        response = client.post("/api/v1/chat/", json=body).json()
        self.llm._generate.assert_called_once()
        self.assertEqual(response["trace"]["answer_mode"], "joint_comparison")
        self.assertIn("2.8 V to 5.5 V", response["trace"]["context"])
        self.assertIn("2.7 V to 3.3 V", response["trace"]["context"])
        self.assertEqual([item["within"] for item in response["trace"]["comparison_calculation"]["checks"]],
                         [True, False])
        self.assertEqual([item["file"] for item in response["citations"]], ["lm75b.pdf", "max30205.pdf"])
        self.llm._generate.return_value = "[1] LM75B pdf 第5页 [2] MAX30205.pdf 第5页"
        response = client.post("/api/v1/chat/", json=body).json()
        self.assertFalse(response["citations"])

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_temperature_step_ratio_is_exact_and_ambiguity_keeps_joint_generation(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        rows = [
            quote("left", "Temperature resolution of 0.125 °C.", 1, doc="lm75b"),
            quote("right", "16-bit (3.90625 mK) Temperature Resolution.", 1, doc="max30205"),
        ]
        body = {"query": "Compare LM75B and MAX30205 temperature steps. Which is smaller and by what factor?",
                "document_ids": ["lm75b", "max30205"], "trace": True}
        client = self.client(rows, "More specifications [1] and [2].")
        response = client.post("/api/v1/chat/", json=body).json()
        self.assertIn("32 倍", response["response"])
        self.assertEqual(response["trace"]["comparison_calculation"]["ratio"], "32")
        self.assertEqual(response["trace"]["answer_mode"], "verified_ratio")
        self.llm._generate.assert_not_called()

        rows.append(quote("another", "Temperature resolution of 250 mK.", .8, doc="lm75b"))
        client = self.client(rows, "Resolution choices exist [1] [2].")
        response = client.post("/api/v1/chat/", json=body).json()
        self.assertIsNone(response["trace"]["comparison_calculation"])
        self.assertEqual(response["trace"]["answer_mode"], "joint_comparison")
        self.llm._generate.assert_called_once()

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_context_coverage_preserves_asked_quantity_outside_the_first_eight_hits(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        self.settings.RAG_EVIDENCE_COVERAGE = True
        rows = [quote(str(i), f"Source {i} has operating limit 3.3 V.", 1) for i in range(9)]
        rows.append(quote("rating", "Absolute maximum rating is +4 V.", .5))
        client = self.client(rows, "3.3 V operating and +4 V absolute [1] [2].")
        result = client.post("/api/v1/chat/", json={
            "query": "Explain the 3.3 V and 4 V ratings. Which limits operating supply?",
            "document_ids": ["ina"], "trace": True,
        }).json()
        trace = result["trace"]
        self.assertEqual(trace["retrieved"][-1]["chunk_id"], "rating")
        self.assertEqual(trace["context_evidence"][1]["chunk_id"], "rating")
        self.assertIn("Absolute maximum rating", trace["context"])

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_uncited_answer_gets_one_retry_without_fabricating_reference(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        client = self.client([quote("p1", "24LC256 supply range is 2.5 V to 5.5 V.", .9)], "")
        data = {"query": "What is the supply range?", "document_ids": ["ina"], "trace": True}
        self.llm._generate.side_effect = ["2.5 V to 5.5 V", "2.5 V to 5.5 V [1]."]
        response = client.post("/api/v1/chat/", json=data).json()
        self.assertIn("2.5 V to 5.5 V", response["response"])
        self.assertEqual(len(response["citations"]), 1)
        self.assertEqual(response["trace"]["model_answer"], "2.5 V to 5.5 V")
        self.assertEqual(self.llm._generate.call_count, 2)
        self.llm._generate.reset_mock()
        self.llm._generate.side_effect = ["2.5 V to 5.5 V", "2.5 V to 5.5 V [9]."]
        response = client.post("/api/v1/chat/", json=data).json()
        self.assertEqual(response["citations"], [])
        self.assertIn("没有足够的证据", response["response"])
        self.assertEqual(self.llm._generate.call_count, 2)

    def test_answer_cleanup_preserves_device_names_and_math_decimals(self):
        llm = LLMClient(self.settings)
        self.assertIn("OPA211 maximum", llm._clean_answer("OPA211 maximum supply is 36 V [1]."))
        self.assertIn("0.0078", llm._clean_answer(r"分辨率为 $0 . 0 0 7 8 ^{\circ} C$ [1]。"))

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_verified_excerpt_preserves_named_scope_and_raw_source(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        rows = [
            quote("other", "20-bit resolution", 1, doc="ADS1114"),
            quote("result", r"The TMP117 provides a 16-bit temperature result with a resolution of $0 . 0 0 7 8 ~ ^ { \circ } \mathsf { C }$", .8, doc="TMP117"),
        ]
        client = self.client(rows, "untrusted generation")
        response = client.post("/api/v1/chat/", json={
            "query": "What is the temperature resolution of the TMP117?",
            "document_ids": ["TMP117"], "trace": True,
        }).json()
        self.assertIn("0.0078°C", response["response"])
        self.assertIn("$0 . 0 0 7 8", response["citations"][0]["text_snippet"])
        self.assertEqual(response["trace"]["answer_mode"], "verified_excerpt")
        self.assertEqual(response["trace"]["context_evidence"][0]["document_id"], "TMP117")
        self.llm._generate.assert_not_called()

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_ambiguous_specs_keep_generation_and_missing_altitude_does_not_guess(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        client = self.client([
            quote("mode1", "10-bit resolution", 1, doc="MCP3008"),
            quote("mode2", "12-bit resolution", .8, doc="MCP3008"),
        ], "The source has two modes [1] [2].")
        query = {"document_ids": ["MCP3008"], "trace": True}
        response = client.post("/api/v1/chat/", json={
            **query, "query": "What is the resolution of the MCP3008 ADC?"
        }).json()
        self.assertEqual(response["trace"]["answer_mode"], "generated")
        self.llm._generate.reset_mock()
        response = client.post("/api/v1/chat/", json={
            **query, "query": "What is the maximum operating altitude of the MCP3008?"
        }).json()
        self.assertEqual(response["trace"]["answer_mode"], "verified_abstention")
        self.assertEqual(response["citations"], [])
        self.llm._generate.assert_not_called()

    def test_verified_passage_leaves_unsupported_notation_to_generation(self):
        altitude = SimpleNamespace(filename="MCP3008.pdf", text="The operating altitude is limited to 2,000 m.")
        self.assertIsNone(find_verified_excerpt("What is the operating altitude of the MCP3008?", [altitude]))
        temperature = SimpleNamespace(
            filename="TMP117.pdf",
            text=r"The TMP117 provides a 16-bit temperature result with a resolution of $0 0 0 7 8 ~ ^ { \circ } \mathsf { C }$",
        )
        self.assertIsNone(find_verified_excerpt("What is the TMP117 temperature resolution?", [temperature]))

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_chat_quotes_both_gain_conditions_and_preserves_the_source(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        raw = "OPA211 Features • Gain bandwidth product: – 80 MHz (G = 100) – 45 MHz (G = 1) • Slew rate: 27 V/µs"
        client = self.client([quote("gain", raw, 1, page=1, doc="OPA211")], "45 MHz when G=1 [1]")
        request = {"query": "OPA211的增益带宽积是多少？", "document_ids": ["OPA211"], "trace": True}
        response = client.post("/api/v1/chat/", json=request).json()
        self.assertEqual(response["trace"]["answer_mode"], "verified_conditions")
        self.assertIn("80 MHz (G = 100) – 45 MHz (G = 1) [1]", response["response"])
        self.assertEqual(response["trace"]["context_evidence"][0]["chunk_id"], "gain")
        self.assertEqual(response["citations"][0]["text_snippet"], raw)
        self.llm._generate.assert_not_called()

        self.settings.RAG_CONDITION_EVIDENCE = False
        response = client.post("/api/v1/chat/", json=request).json()
        self.assertEqual(response["trace"]["answer_mode"], "generated")
        self.llm._generate.assert_called()

    def test_conditional_supply_requires_full_conditions_and_named_device(self):
        question = "What is the operating power-supply range of the TMP117?"
        summary = SimpleNamespace(filename="TMP117.pdf", text="The TMP117 operates from 1.7 V to 5.5 V.")
        full = SimpleNamespace(
            filename="TMP117.pdf",
            text="Over free-air temperature range and V+ = 1.7 V to 5.5 V for "
                 "T<sub>A</sub> = −55 °C to 70 °C, or V+ = 1.8 V to 5.5 V for TA = −55 °C to 150 °C.",
        )
        self.assertIsNone(find_condition_excerpt(question, [summary]))
        proof = find_condition_excerpt(question, [summary, full])
        self.assertIs(proof.chunk, full)
        self.assertIn("1.7 V to 5.5 V for TA = -55 °C to 70 °C", proof.excerpt)
        self.assertIn("1.8 V to 5.5 V for TA = -55 °C to 150 °C", proof.excerpt)
        self.assertIsNone(find_condition_excerpt(question.replace("TMP117", "TMP116"), [full]))
        self.assertIsNone(find_condition_excerpt(question.replace("range", "range at 125 °C"), [full]))
        self.assertIsNone(find_condition_excerpt(question.replace("operating", "absolute maximum"), [full]))
        self.assertIsNone(find_condition_excerpt(question, [full, SimpleNamespace(
            filename=full.filename, text=full.text.replace("1.8 V", "2 V"),
        )]))

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_chat_recovers_supply_range_from_document_scoped_bm25(self, rerank_post):
        def rerank(*args, **kwargs):
            documents = kwargs["json"]["documents"]
            response = Mock()
            response.json.return_value = {"results": [
                {"index": index, "relevance_score": 0 if "Supply range" in text else 1}
                for index, text in enumerate(documents)
            ]}
            return response

        rerank_post.side_effect = rerank
        self.settings.EVAL_TRACE_ENABLED = True
        rows = [quote(str(index), f"Device description section {index}", 1, doc="TMP117")
                for index in range(13)]
        raw = (r"5 - \mu A ,$ 1-Hz conversion cycle • Supply range: – 1.7 V to 5.5 V "
               r"from $- 5 5 \; ^ { \circ } \mathbf { C }$ "
               r"to $7 0 \; ^ { \circ } \mathbf { C }$ – 1.8 V to 5.5 V from "
               r"$- 5 5 \; ^ { \circ } \mathbf { C }$ to $1 5 0 \; ^ { \circ } \mathbf { C }$ "
               r"• 16-bit resolution: $0 .")
        supply = quote("supply", raw, .5, doc="TMP117")
        client = self.client([*rows, supply], "A general reply [1]")
        self.container.bm25_store.add_documents(
            [item.text for item in [*rows, supply]],
            [item.metadata for item in [*rows, supply]],
            [item.id for item in [*rows, supply]],
        )
        response = client.post("/api/v1/chat/", json={
            "query": "TMP117的供电电压范围是多少？", "document_ids": ["TMP117"], "trace": True,
        }).json()
        self.assertEqual(response["trace"]["answer_mode"], "verified_conditions")
        self.assertNotIn("supply", [r["chunk_id"] for r in response["trace"]["retrieved"]])
        self.assertEqual(response["trace"]["condition_candidates"][0]["chunk_id"], "supply")
        self.assertEqual(response["citations"][0]["text_snippet"], raw)
        self.assertIn("1.8 V to 5.5 V from -55°C to 150°C", response["response"])
        self.llm._generate.assert_not_called()

    def test_broad_conditional_gain_rejects_composite_queries_and_longer_lists(self):
        question = "What is the gain bandwidth product of the OPA211?"
        first = "Gain bandwidth product: 80 MHz (G = 100) – 45 MHz (G = 1)"
        source = SimpleNamespace(filename="OPA211.pdf", text=first)
        self.assertIsNone(find_condition_excerpt(question.replace("?", " at G=1?"), [source], exact_gain=False))
        self.assertIsNone(find_condition_excerpt(question.replace("?", ", and its slew rate?"), [source]))
        self.assertIsNone(find_condition_excerpt(question, [SimpleNamespace(
            filename=source.filename, text=first + " – 60 MHz (G = 10)",
        )]))
        self.assertIsNone(find_condition_excerpt(question, [SimpleNamespace(
            filename=source.filename, text=first.replace("G = 1)", "G = 100)"),
        )]))

    def test_conditional_gain_reads_complete_advanced_table_rows(self):
        text = (
            "GBW | Gain-bandwidth product | G = 100 [跨2列合并] | | 80 [跨3列合并] | | | MHz "
            "GBW | Gain-bandwidth product | G = 1 [跨2列合并] | | 45 [跨3列合并] | | | MHz"
        )
        question = "What is the gain bandwidth product of the OPA211?"
        path = "OPA211.pdf"
        proof = find_condition_excerpt(question, [SimpleNamespace(filename=path, text=text)])
        self.assertIn("80 MHz (G = 100) – 45 MHz (G = 1)", proof.excerpt)
        self.assertIsNone(find_condition_excerpt(question, [SimpleNamespace(
            filename=path, text=text.replace("80 [", "80 | 20 ["),
        )]))

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_chat_returns_only_the_requested_gain_row(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        other = "GBW | Gain-bandwidth product | G = 100 | | 80 | | MHz"
        target = "GBW | Gain-bandwidth product | G = 1 | | 45 | | MHz"
        client = self.client([
            quote("table", f"{other}\n{target}", 1, doc="OPA211"),
            quote("wrong-gain", other, .9, doc="OPA211"),
            quote("exact-row", target, .8, doc="OPA211"),
        ], "45 MHz and 80 MHz [1]")
        request = {"query": "OPA211在G=＋1.0时的GBW是多少？", "document_ids": ["OPA211"], "trace": True}
        result = client.post("/api/v1/chat/", json=request).json()
        self.assertEqual(result["trace"]["answer_mode"], "verified_gain")
        self.assertIn("(G = 1): 45 MHz", result["response"])
        self.assertNotIn("80 MHz", result["response"])
        self.assertEqual(result["trace"]["context_evidence"][0]["chunk_id"], "exact-row")
        self.assertEqual(result["citations"][0]["text_snippet"], target)
        self.llm._generate.assert_not_called()

        self.settings.RAG_EXACT_GAIN_EVIDENCE = False
        result = client.post("/api/v1/chat/", json=request).json()
        self.assertEqual(result["trace"]["answer_mode"], "generated")
        self.llm._generate.assert_called()

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_chat_requires_one_consistent_value_at_the_requested_gain(self, rerank_post):
        rerank_post.return_value.json.return_value = {"results": []}
        self.settings.EVAL_TRACE_ENABLED = True
        first = "Gain Bandwidth Product | GBP | A<sub>V</sub> = 100 | | 18 | | MHz"
        second = "Gain Bandwidth Product | GBP | $A_V$ =100 | 16 | | | MHz"
        client = self.client([
            quote("one-voltage", first, 1, doc="ADA4625-1"),
            quote("another-voltage", second, .9, doc="ADA4625-1"),
            quote("different-parameter", "Unity-Gain Crossover | UGC | A_V = 1 | | 12.4 | | MHz", .8, doc="ADA4625-1"),
        ], "A guessed answer [1]")
        for gain, reason in [("100", "conflicting_gain_evidence"), ("1", "gain_evidence_missing")]:
            with self.subTest(gain=gain):
                result = client.post("/api/v1/chat/", json={
                    "query": f"What is GBP of the ADA4625-1 at A_V = {gain}?",
                    "document_ids": ["ADA4625-1"], "trace": True,
                }).json()
                self.assertEqual(result["trace"]["answer_mode"], "verified_abstention")
                self.assertEqual(result["trace"]["verified_excerpt"]["reason"], reason)
                self.assertEqual(result["citations"], [])
        self.llm._generate.assert_not_called()

    def test_exact_gain_canonicalizes_units_and_rejects_other_constraints(self):
        text = "Gain bandwidth product: 18 MHz (G = -2) – 20 MHz (G = 2) – 22 MHz (G = 3)"
        text += "\nGBP | Gain Bandwidth Product | G = -2 | | 18000 | | kHz"
        source = SimpleNamespace(filename="UOA234.pdf", text=text)
        question = "What is the GBP of the UOA234 at G=-2?"
        proof = find_condition_excerpt(question, [source])
        self.assertIn("G = -2", proof.excerpt)
        self.assertIn("18000 kHz", proof.excerpt)
        for suffix in (" and its slew rate", " at 40 °C", " to +2", " dB"):
            self.assertIsNone(find_condition_excerpt(question.replace("?", suffix + "?"), [source]))

    def test_image_path_is_bound_to_its_storage_directory(self):
        root = Path(self.temp.name)
        allowed = root / "circuit_images"
        allowed.mkdir()
        image = allowed / "sample.png"
        image.write_bytes(b"png test fixture")
        outside = root / "other.png"
        outside.write_bytes(b"outside test fixture")
        self.assertEqual(_resolve_image_path(str(image), self.settings), image.resolve())
        with self.assertRaises(FileNotFoundError):
            _resolve_image_path(str(outside), self.settings)

    def test_bad_sqlite_source_is_preserved_after_failed_recovery(self):
        path = Path(self.temp.name) / "unreadable.db"
        original = b"unreadable source file"
        path.write_bytes(original)
        with self.assertRaises(SQLiteCorruptedError):
            TableStore(str(path))
        self.assertEqual(path.read_bytes(), original)

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_persisted_bm25_and_table_lookup_survive_rerank_failure(self, rerank_post):
        rerank_post.side_effect = TimeoutError("reranker offline")
        store = BM25Store(self.settings, str(self.settings.DATA_DIR / "bm25_index.pkl"))
        rows = ["register F380 step", "other circuit", "amplifier log", "different topic"]
        store.add_documents(rows, [{"page": 13, "document_id": "tmp"}] * len(rows),
                            ["f380", "other", "amp", "noise"])
        store.save()
        table_data = [["Parameter", "MIN", "TYP", "MAX", "UNIT"],
                      ["Supply current", "", "150", "200", "µA"]]
        for doc, page in [("tmp", 13), ("wrong-doc", 13), ("tmp", 14)]:
            self.container.table_store.add({"document_id": doc, "page": page, "data": table_data})
        client = self.client([], "F380 [1]")
        response = client.post("/api/v1/chat/", json={
            "query": "Read F380", "document_ids": ["tmp"],
        }).json()
        self.assertIn("F380", response["citations"][0]["text_snippet"])
        results = asyncio.run(self.container.table_store.search(
            "What is ADS1115 supply current?", filters={"document_ids": ["tmp"], "page": 13}
        ))
        self.assertEqual([(row["document_id"], row["page"]) for row in results], [("tmp", 13)])
        rendered = self.container.retriever._format_table_text(results[0]["table_data"])
        self.assertIn("Supply current |  | 150 | 200 | µA", rendered)

    def test_html_and_chunking_preserve_enumeration_and_ambiguous_cell(self):
        html = (
            "<table><tr><th>Parameter</th><th>MIN</th><th>TYP</th><th>MAX</th><th>UNIT</th></tr>"
            "<tr><td>DR</td><td colspan='3'>8, 16, 32, 64, 128, 250, 475, 860</td><td>SPS</td></tr>"
            "<tr><td rowspan='2'>Supply current</td><td></td><td>150</td><td>200</td><td>µA</td></tr>"
            "<tr><td colspan='3'>0.15 0.5</td><td>µA</td></tr></table>"
        )
        _, rows, plain = _parse_table_content(html)
        self.assertEqual(rows[2], ["Supply current", "", "150", "200", "µA"])
        self.assertEqual(rows[3][0], "Supply current")
        self.assertIn("0.15 0.5 [跨3列合并]", rows[3][1])
        pages = [{"page_number": 5, "tables": [{"plain_text": plain}]}]
        chunks = _build_chunks_from_pages(pages, self.settings, "ads", "ads.pdf")
        rates = next(row for row in chunks if "475" in row["content"])
        self.assertEqual(rates["metadata"]["source"], "table_text")
        self.assertIn("MIN | TYP | MAX", rates["metadata"]["table_header"])
        self.assertIn("8, 16, 32, 64, 128, 250, 475, 860 [跨3列合并]", rates["content"])

    def test_mineru_equations_and_definitions_reach_one_ordered_index_chunk(self):
        expression = r"Y = \frac{a - b}{2^{n}}"
        definitions = r"\begin{array}{ll} a &= \text{Measured value.} \\ b &= \text{Reference.} \end{array}"
        content = [
            ("header", "Unrelated running header"),
            ("paragraph_title", "Reference transfer"),
            ("text", "Use the nominal reference of 500 mV."),
            ("paragraph_title", "Equation 2:"),
            ("equation", expression), ("text", "Where:"), ("equation", definitions),
            ("text", "Calibration uses another instrument."),
            ("paragraph_title", "Other property"), ("text", "Amplifier output."),
            ("equation", "A = B + C"), ("text", "(3)"),
            ("table", "| Pin | State |\n|---|---|\n| 4 | Low |"),
            ("footer", "Unrelated footer"),
        ]
        blocks = [{"type": kind, "content": value} for kind, value in content]
        structured = {"pages": [{"blocks": blocks}], "extensions": {"mineru": {"tier": "advanced"}}}
        pages, _, _ = MinerUClient(self.settings)._build_pages(structured)
        self.settings.MINERU_VERIFY_TABLE_COLUMNS = False
        self.settings.CHUNK_SIZE = 55
        self.settings.CHUNK_OVERLAP = 12
        with patch.object(MinerUClient, "parse_pdf", return_value={
            "pages": [pages[0].to_dict()], "status": "success", "structured_content": structured,
        }):
            adapted = EnhancedDocumentProcessor._parse_pdf_mineru(
                SimpleNamespace(settings=self.settings), Path("example.pdf"),
            )["pages"]
        chunks = _build_chunks_from_pages(adapted, self.settings, "example", "example.pdf")
        formulae = [row for row in chunks if row["metadata"].get("formula")]
        self.assertEqual(len(formulae), 2)
        self.assertEqual(formulae[0]["content"], "\n\n".join(value for _, value in content[1:7]))
        self.assertEqual(formulae[1]["content"], "\n\n".join(value for _, value in content[8:12]))
        self.assertEqual(formulae[0]["metadata"]["page"], 1)
        self.assertLess(formulae[0]["metadata"]["char_end"], formulae[1]["metadata"]["char_start"])
        for literal in (expression, definitions, "A = B + C"):
            self.assertEqual(sum(literal in row["content"] for row in chunks), 1)
        self.assertIn("Calibration uses another instrument.", " ".join(row["content"] for row in chunks))
        self.assertTrue(any(row["metadata"]["source"] == "table_text" for row in chunks))
        self.assertNotIn("Unrelated running header", adapted[0].text)

    def test_pdf_geometry_requires_matching_condition_and_distinct_columns(self):
        path = Path(self.temp.name) / "columns.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page(width=600, height=300)
            for heading, x in zip(("MIN", "TYP", "MAX"), (360, 410, 460)):
                page.insert_text((x - fitz.get_text_length(heading), 55), heading)
            page.insert_text((45, 83), "Operating TA = 25C")
            page.insert_text((410 - fitz.get_text_length("150"), 83), "150")
            page.insert_text((460 - fitz.get_text_length("200"), 83), "200")
            page.insert_text((45, 110), "Shutdown TA = 25C")
            page.insert_text((410 - fitz.get_text_length("0.5"), 110), "0.5")
            page.insert_text((460 - fitz.get_text_length("2"), 110), "2")
            pdf.save(path)
        rows = [
            ["Parameter", "Condition", "MIN TYP MAX", "UNIT"],
            ["Operating", "TA = 25C", "150 200", "uA"],
            ["Shutdown", "TA = 70C", "0.5 2", "uA"],
            ["Data rate", "", "8, 16, 32", "SPS"],
            ["Drain current", "", "-14.9-11.9", "A"],
        ]
        annotated, proof = annotate_spec_columns(path, 1, rows, [0, 0, 1, 1])
        self.assertEqual(annotated[1][2], "PDF verified: TYP = 150; MAX = 200")
        self.assertEqual(annotated[2][2], "Unverified column: 0.5 2")
        self.assertEqual(annotated[3][2], "Enumerated options: 8, 16, 32")
        self.assertEqual(annotated[4][2], "Unverified column: -14.9-11.9")
        self.assertEqual([c["column"] for c in proof[1]["cells"]], ["TYP", "MAX"])
        repeated, proof = annotate_spec_columns(path, 1, [*rows, rows[1]], [0, 0, 1, 1])
        self.assertEqual(proof, {})
        self.assertEqual(repeated[1][2], "Unverified column: 150 200")

    @patch("retrieval.hybrid_retriever.requests.post")
    def test_preserves_shared_parameter_with_different_conditions(self, rerank_post):
        header = "Gain bandwidth product | " + "Gain tested under stated product conditions. " * 3
        rows = [
            quote("g1", f"{header} G=1 | 45 MHz", .9, page=9, doc="opa"),
            quote("g100", f"{header} G=100 | 80 MHz", .8, page=9, doc="opa"),
        ]
        rerank_post.return_value.json.return_value = {"results": []}
        client = self.client(rows, "G=100: 80 MHz [2]. G=1: 45 MHz [1].")
        response = client.post("/api/v1/chat/", json={
            "query": "给出 OPA211 在 G=100 和 G=1 时的增益带宽积", "document_ids": ["opa"],
        }).json()
        self.assertEqual([c["text_snippet"] for c in response["citations"]], [rows[1].text, rows[0].text])


if __name__ == "__main__":
    unittest.main(verbosity=2)

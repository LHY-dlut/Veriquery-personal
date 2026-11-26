"""
LLM client — unified interface for RAG answer generation via Xinference API.

Answer generation is delegated to the Xinference platform (model UID
"qwen3.5" = Qwen3.5-0.8B) through the OpenAI-compatible
/v1/chat/completions endpoint. No local model weights are loaded.

Public interface is identical to the previous local HuggingFace
implementation (generate_answer / invoke), so callers
(agents/workflow_nodes.py, reasoning/*) require no changes.

Usage:
    from core.llm_client import get_llm_client
    client = get_llm_client(settings)
    answer = client.generate_answer(query="NE5532供电电压?", context="...")
"""


import logging
import re
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class LLMClient:
    """Xinference-backed LLM client with RAG answer generation.

    The client is stateless apart from API configuration; generation
    quality controls (prompt engineering, answer cleaning, low-temperature
    retry) are preserved from the local implementation.
    """

    def __init__(self, settings):
        self.settings = settings
        self.api_base = str(settings.XINFERENCE_API_BASE).rstrip("/")
        self.model_uid = settings.XINFERENCE_LLM_MODEL
        self.timeout = settings.XINFERENCE_TIMEOUT
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if settings.XINFERENCE_API_KEY:
            self._headers["Authorization"] = f"Bearer {settings.XINFERENCE_API_KEY}"
        logger.info(
            f"LLMClient initialized (Xinference API, model={self.model_uid}, "
            f"base={self.api_base}, timeout={self.timeout}s)"
        )

    def _generate(self, prompt: str, max_new_tokens: int, temperature: float,
                  transcription: bool = False) -> str:
        """Call /v1/chat/completions with a single user message.

        Retries once on network errors and 5xx responses; 4xx errors fail
        immediately.

        Args:
            prompt: Input text for the model.
            max_new_tokens: Max tokens to generate (hard cap at 256, same
                as the local implementation; raised to 1024 for verbatim
                transcription requests, which need room for line-by-line
                output).
            temperature: Sampling temperature (hard cap at 0.7, same as
                the local implementation).
            transcription: Whether this is a verbatim-transcription request.

        Returns:
            Generated text (unprocessed).

        Raises:
            RuntimeError: If the API fails after retry.
        """
        token_cap = 1024 if transcription else 256
        payload = {
            "model": self.model_uid,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "max_tokens": min(max_new_tokens, token_cap),
        }
        if temperature > 0:
            payload["temperature"] = min(temperature, 0.7)
            payload["top_p"] = 0.9

        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = requests.post(
                    f"{self.api_base}/chat/completions",
                    headers=self._headers,
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    message = resp.json()["choices"][0]["message"]
                    content = message.get("content", "")
                    if isinstance(content, list):
                        content = "".join(
                            seg.get("text", "") for seg in content if isinstance(seg, dict)
                        )
                    return (content or "").strip()

                error = RuntimeError(
                    f"LLM API HTTP {resp.status_code}: {resp.text[:300]}"
                )
                if 400 <= resp.status_code < 500:
                    raise error
                last_error = error
            except (requests.RequestException, ValueError, KeyError, IndexError) as e:
                last_error = RuntimeError(f"LLM API request failed: {e}")

            if attempt == 0:
                logger.warning(f"LLM API attempt 1 failed ({last_error}), retrying in 1s...")
                time.sleep(1.0)

        raise RuntimeError(f"LLM API failed after retry: {last_error}")

    def generate_answer(
        self,
        query: str,
        context: str,
        max_new_tokens: int = None,
        temperature: float = None,
        transcription: bool = False,
    ) -> str:
        """Generate an answer for a RAG query using context and prompt engineering.

        Pipeline: _build_prompt() → _generate() → _clean_answer()

        Args:
            query: User question.
            context: Retrieved context text.
            max_new_tokens: Override for max generation tokens (defaults to settings).
            temperature: Override for sampling temperature (defaults to settings).
            transcription: Verbatim-transcription request — relaxes the
                brevity rules and raises the output token cap.

        Returns:
            Cleaned answer text.
        """
        if max_new_tokens is None:
            max_new_tokens = self.settings.LLM_MAX_TOKENS
        if temperature is None:
            temperature = self.settings.LLM_TEMPERATURE

        if max_new_tokens <= 0 or max_new_tokens > 2048:
            max_new_tokens = self.settings.LLM_MAX_TOKENS
        if temperature < 0 or temperature > 2.0:
            temperature = self.settings.LLM_TEMPERATURE

        try:
            prompt = self._build_prompt(query, context, transcription)
            answer = self._generate(prompt, max_new_tokens, temperature, transcription)
            if transcription:
                # 原文转录要求保真:通用清洗会重组句子、去重、过滤短行,
                # 会破坏"逐行转录"的结构,这里只做最小化处理
                answer = answer.strip()
            else:
                answer = self._clean_answer(answer)
            if not answer or len(answer.strip()) < 5:
                logger.warning("LLM首次生成答案为空或过短，降低温度重试")
                answer = self._generate(prompt, max_new_tokens, max(0.1, temperature - 0.2), transcription)
                answer = answer.strip() if transcription else self._clean_answer(answer)
            return answer
        except Exception as e:
            logger.error(f"LLM answer generation failed: {e}")
            raise

    def _build_prompt(self, query: str, context: str, transcription: bool = False) -> str:
        """Build the RAG prompt.

        The prompt is deliberately short: the serving model (Qwen3.5-0.8B)
        follows long, heavily-constrained prompts poorly — with a verbose
        template it stopped matching questions like "第8页的标题和日期"
        to the retrieved text and answered "文档中未找到相关内容" even
        though the answer was present in the context. A compact prompt
        with the same rules keeps the model answering from the context.

        Args:
            query: User question.
            context: Retrieved context text.
            transcription: Verbatim-transcription request — line-by-line
                output, no summarising.
        """
        chip_pattern = re.compile(
            r'('
            r'(?:NE|LM|SN|CD|STM32|AD|MAX|TL|OP|UA|MC|ATmega|ATtiny)[A-Z0-9.-]+'
            r'|\d{2}[A-Z]{2,}\d+[A-Z0-9-]*'
            r')',
            re.IGNORECASE,
        )
        chip_match = chip_pattern.search(query)
        chip_name = chip_match.group(1).upper() if chip_match else None

        # 只有"问参数"类问题才强调型号;对"首页标题是什么"这类元信息
        # 问题,强制型号开头会把答案带偏。
        param_intent = any(kw in query.lower() for kw in (
            '电压', '电流', '功耗', '温度', '频率', '带宽', '范围', '参数', '额定',
            '最大值', '最小值', '典型值', 'supply', 'voltage', 'current', 'power',
            'temperature', 'frequency', 'bandwidth', 'range', 'rating', 'typical',
            'maximum', 'minimum',
        ))

        chip_line = ""
        if chip_name and param_intent:
            chip_line = f"用户询问的型号是 {chip_name}，请使用该准确型号作答。\n"

        if transcription:
            rules = ('严格按资料原文作答(表格逐行转录)，不要改写、总结或补充；'
                     '数值带正确单位；若资料中确实没有相关信息，直接说明"文档中未找到相关内容"。')
        elif param_intent:
            rules = ('从资料中找到该参数对应的数值并作答，数值与单位照抄原文；'
                     '资料排版混乱时给出你识别到的最匹配数值并标注页码；'
                     '若同一参数给出的是范围(如 -55 与 150 两个值):'
                     '问"上限/最大/最高"时只回答较大的那个值,问"下限/最小"时只回答较小的那个值,'
                     '不要把两个值都写进答案;'
                     '确实没有该参数时说明"文档中未找到相关内容"。')
        else:
            rules = ('只依据资料作答，不要编造；数值带正确单位；回答简洁；'
                     '若资料中确实没有相关信息，直接说明"文档中未找到相关内容"。')

        return f"""你是电子元器件技术文档助手。根据下面的技术资料回答用户问题。

【资料】
{context}

【问题】
{query}

{chip_line}要求：{rules}

答案："""

    def _clean_answer(self, answer: str) -> str:
        """Clean LLM output: remove format noise, boilerplate, and duplicate sentences.

        Steps:
            1. Remove <think...</think > blocks
            2. Remove thinking/analysis prefixes and their entire block
            3. Truncate conversation markers (assistant/user/system)
            4. Strip Markdown/HTML formatting
            5. Remove boilerplate prefixes
            6. Decode HTML entities
            7. Deduplicate sentences via 20-char prefix hash
            8. Fallback to safe cleaning if aggressive cleaning removes too much content
        """
        raw_answer = answer

        answer = re.sub(r'<think\s*>.*?</think\s*>', '', answer, flags=re.DOTALL | re.IGNORECASE)

        thinking_prefixes = [
            r'\d*\s*Thinking\s*Process\s*:\s*',
            r'\d*\s*Analyze\s+the\s+Request\s*:\s*',
            r'\d*\s*Let(?:\'s|\s+me)\s+(?:think|analyze|reason|break\s+down)\s*[:：]?\s*',
            r'\d*\s*Step\s*\d+\s*[:：]\s*Analysis\s*',
        ]
        for prefix in thinking_prefixes:
            m = re.match(prefix, answer, re.IGNORECASE)
            if m:
                after_prefix = answer[m.end():]
                actual_answer_match = re.search(
                    r'(?:^|\n)\s*('
                    r'(?:NE|LM|SN|CD|STM32?|AD|MAX|TL|OP|UA|MC|AT)\w*(?:的|是|工作|电源|电压|供电|输入|输出|最大|最小|范围|绝对)'
                    r'|根据.*?(?:规格|数据|资料|文档|手册)'
                    r'|[±]?\d+\.?\d*\s*[VvAa][\s，,。.；;]'
                    r'|该(?:芯片|器件|元件)'
                    r'|Supply\s+voltage'
                    r')',
                    after_prefix, re.IGNORECASE,
                )
                if actual_answer_match:
                    start_pos = actual_answer_match.start()
                    if after_prefix[start_pos] == '\n':
                        start_pos += 1
                    answer = after_prefix[start_pos:]
                else:
                    answer = ""
                break

        answer = re.sub(
            r'^\d*\s*\*\s*(?:Step|Role|Task|Question|Analysis|Reasoning|User\s+Question)\s*\d*\s*[:：].*$',
            '', answer, flags=re.IGNORECASE | re.MULTILINE,
        )

        for stop_word in ["\n\n\n"]:
            if stop_word in answer:
                idx = answer.find(stop_word)
                answer = answer[:idx]

        for stop_word in ["assistant:", "user:", "system:"]:
            lower = answer.lower()
            idx = lower.find(stop_word)
            if idx > 20:
                answer = answer[:idx]

        idx_assistant = answer.lower().find("assistant")
        if idx_assistant > 0 and idx_assistant < len(answer) - 12:
            after = answer[idx_assistant:idx_assistant + 12].lower()
            if after in ("assistant", "assistant\n", "assistant "):
                answer = answer[:idx_assistant]

        for marker in ["###", "【答案】"]:
            lower = answer.lower()
            idx = lower.find(marker.lower())
            if idx > 30:
                answer = answer[:idx]

        answer = re.sub(r'^(?:Wait|Oh\s+wait|Let\s+me\s+think|I\s+need\s+to)[,.]?\s+', '', answer, flags=re.IGNORECASE)
        # 只剥离列表编号(1. / 2) / 1、),绝不能删掉数值前的负号或数字本身:
        # 旧规则 '^\d+\s+(?=[A-Za-z一-鿿])' 会把 "12 V" 变成 "V",
        # lstrip('\n*-. ') 会把 "-30 V" 变成 "30 V" —— 模型答对也会被改错
        answer = re.sub(r'^\d{1,2}\s*[.)、]\s+', '', answer)

        answer = answer.lstrip('\n*• \t')

        answer = re.sub(r"\*\*", "", answer)
        answer = re.sub(r"<[^>]+>", "", answer)
        answer = re.sub(r"```markdown\s*", "", answer)
        answer = re.sub(r"```\s*", "", answer)
        answer = re.sub(r'^\s*(?:Solution|Answer|Result)\s*[:：]?\s*$', '', answer, flags=re.IGNORECASE | re.MULTILINE)

        answer = re.sub(r"根据.*?分析如下：", "", answer, flags=re.IGNORECASE | re.DOTALL)
        answer = re.sub(r"作为[^。！？]+专家[^。！？]*[，,。！？]", "", answer, flags=re.IGNORECASE)
        answer = re.sub(r"我需基于[^。！？]+[。！？]", "", answer, flags=re.IGNORECASE)
        answer = re.sub(r"以下是针对[^。！？]+[：:]", "", answer, flags=re.IGNORECASE)

        answer = re.sub(r'(\d+)\.\s+(\d+)', r'\1.\2', answer)
        answer = re.sub(r'(\d+)\.\n(\d+)', r'\1.\2', answer)
        answer = re.sub(r'(\d+)\s+(V|A|℃|°C|mA|mV|μA|kHz|MHz)', r'\1\2', answer, flags=re.IGNORECASE)
        answer = re.sub(r'(\d+)\.\s*(V|A|℃|°C|mA|mV|μA|kHz|MHz)', r'\1\2', answer, flags=re.IGNORECASE)

        html_entities = {
            "&lt;": "<",
            "&gt;": ">",
            "&amp;": "&",
            "&quot;": '"',
            "&apos;": "'",
        }
        for entity, char in html_entities.items():
            answer = answer.replace(entity, char)

        # 句子切分:英文句点会切开小数("12.0 A" → "12." + "0 A"),
        # 导致数值片段被后续长度过滤丢弃。句点仅在前后都不是数字时切分。
        sentences = re.split(r'(?<=[。！？!?])|(?<!\d)\.(?!\d)|\n', answer)
        seen = set()
        unique_sentences = []

        for sent in sentences:
            sent_stripped = sent.strip()
            if sent_stripped and len(sent_stripped) > 1:
                sent_normalized = re.sub(r'\s+', '', sent_stripped.lower())
                if len(sent_normalized) > 1:
                    # 去重 hash 取前 40 字符:过短(如 20)会把
                    # "…最大值为 12.0 A" 与 "…最大值为 12.6 A" 误判为重复
                    similarity_hash = sent_normalized[:40]
                    if similarity_hash not in seen:
                        seen.add(similarity_hash)
                        unique_sentences.append(sent_stripped)

        answer = '\n'.join(unique_sentences[:12])
        answer = re.sub(r"\n\s*\n", "\n", answer)
        cleaned = answer.strip()

        if len(cleaned) < 10 and raw_answer and len(raw_answer.strip()) > 10:
            has_thinking_prefix = bool(re.match(
                r'\d*\s*(?:Thinking\s*Process|Analyze\s+the\s+Request|Let\s+me\s+think)',
                raw_answer, re.IGNORECASE,
            ))
            if has_thinking_prefix:
                logger.warning(f"_clean_answer 检测到纯thinking block且无实际答案，返回空")
                cleaned = ""
            else:
                logger.warning(f"_clean_answer 清理后过短({len(cleaned)} chars), 使用安全降级清理")
                safe = raw_answer
                for sw in ["assistant", "user:", "system:", "\n\n\n"]:
                    if sw in safe.lower():
                        idx = safe.lower().find(sw)
                        if idx > 3:
                            safe = safe[:idx]
                            break
                safe = re.sub(r'^\s*(?:Thinking\s*Process|Analyze\s+the\s+Request)\s*:\s*', '', safe, flags=re.IGNORECASE)
                safe = re.sub(r'<think\s*>.*?</think\s*>', '', safe, flags=re.DOTALL | re.IGNORECASE)
                safe = re.sub(r"\*\*", "", safe)
                safe = re.sub(r"<[^>]+>", "", safe)
                safe = re.sub(r"```\w*\s*", "", safe)
                safe = safe.lstrip('\n*• \t')
                if len(safe.strip()) > len(cleaned):
                    cleaned = safe.strip()

        return cleaned

    def invoke(self, prompt: str) -> str:
        """Direct LLM call compatible with LangChain invoke protocol.

        Unlike generate_answer(), this passes the prompt straight to the model
        without template construction or answer cleaning.

        Args:
            prompt: Fully constructed prompt text.

        Returns:
            Raw model output (unprocessed).
        """
        try:
            return self._generate(
                prompt,
                max_new_tokens=self.settings.LLM_MAX_TOKENS,
                temperature=self.settings.LLM_TEMPERATURE,
            )
        except Exception as e:
            logger.error(f"LLM invoke failed: {e}")
            raise


_llm_client_instance: Optional[LLMClient] = None
_llm_client_lock = threading.Lock()


def get_llm_client(settings) -> LLMClient:
    """Return the thread-safe LLMClient singleton via double-checked locking.

    Args:
        settings: Settings object (only used on first call).

    Returns:
        The global LLMClient instance.
    """
    global _llm_client_instance

    if _llm_client_instance is None:
        with _llm_client_lock:
            if _llm_client_instance is None:
                _llm_client_instance = LLMClient(settings)

    return _llm_client_instance

from __future__ import annotations

import os
import json
import inspect
import asyncio
import hashlib
import re
from typing import Any, Awaitable, Callable

import httpx

from app import db
from app.services.network_policy import require_outbound_network, validate_outbound_http_url
from app.services.secret_store import secret_store


class ModelGateway:
    """Dispatch model requests to the configured provider or local fallback."""

    async def summarize(self, prompt: str, context: dict[str, Any] | None = None, model_config_id: str = "deterministic") -> str:
        if not model_config_id or model_config_id == "deterministic":
            return self._deterministic_summary(prompt, context or {})
        row = db.query_one("SELECT * FROM model_configs WHERE id = ? AND enabled = 1", (model_config_id,))
        if not row:
            raise RuntimeError(f"模型配置不存在或未启用: {model_config_id}")
        if row["provider"] not in {"openai", "openai_compatible"}:
            raise RuntimeError(f"暂不支持模型供应商: {row['provider']}")
        require_outbound_network("在线模型调用")
        base_url = self._validated_base_url(row)
        api_key = self._api_key(row)
        config = db.json_loads(row.get("config_json"), {})
        system = str((context or {}).get("system_prompt") or "你是平台级智能体，请准确、简洁地完成用户任务。")
        payload = {
            "model": row["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": config.get("temperature", 0.2),
        }
        timeout = float(config.get("timeout", 90))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(f"{base_url}/chat/completions", json=payload, headers={"Authorization": f"Bearer {api_key}"})
            response.raise_for_status()
            data = response.json()
        return str(data["choices"][0]["message"]["content"])

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        """Turn a conversational follow-up into a standalone, structured request."""
        if not model_config_id or model_config_id == "deterministic":
            return {
                "standalone_request": message,
                "intent": "general",
                "parameters": {},
                "missing_information": [],
                "is_follow_up": False,
                "source": "direct",
            }
        compact_history = [
            {"role": item["role"], "content": str(item["content"])[:4000]}
            for item in history[-12:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        prompt = (
            "对话历史：\n"
            + json.dumps(compact_history, ensure_ascii=False)
            + "\n\n用户最新消息：\n"
            + message
            + "\n\n只输出一个 JSON 对象，不要 Markdown。字段必须为："
              "standalone_request（补全上下文后的独立任务，保留本次任务中的关键实体、时间、格式、文件名和约束；若用户切换话题，不得带入旧意图）、"
              "intent（简短英文标识）、parameters（对象，使用简短稳定的字段名；地点可统一用 city，天气相对日期用 day，输出格式用 format，其他字段按当前任务原意命名）、"
              "missing_information（数组）、is_follow_up（布尔值）。"
              "missing_information 只能填写缺少后任务就无法执行的严格必填项；日期、预算、偏好、人数等可采用合理默认的信息不要列入。"
              "missing_information 中每一项只能是简短字段名或名词短语，不得写分析、推理、提问句、JSON 或内部指令。"
              "不要回答问题，只做意图还原。"
        )
        raw = await self.summarize(
            prompt,
            {"system_prompt": "你是智枢平台的上下文意图解析器。必须忠实还原用户当前真正要完成的任务，禁止执行任务。"},
            model_config_id=model_config_id,
        )
        parsed = self._json_object(raw)
        standalone = self._sanitize_standalone_request(str(parsed.get("standalone_request") or message), message)
        parameters = parsed.get("parameters") if isinstance(parsed.get("parameters"), dict) else {}
        parameters = self._normalize_parameters(parameters, standalone)
        missing = parsed.get("missing_information") if isinstance(parsed.get("missing_information"), list) else []
        missing = [
            item.strip()[:160]
            for item in missing[:20]
            if isinstance(item, str) and item.strip()
        ]
        return {
            "standalone_request": standalone or message,
            "intent": str(parsed.get("intent") or "general")[:80],
            "parameters": parameters,
            "missing_information": missing,
            "is_follow_up": bool(parsed.get("is_follow_up")),
            "source": "model",
        }

    def _sanitize_standalone_request(self, standalone: str, original_message: str) -> str:
        """Remove response-format instructions accidentally copied from the parser prompt."""
        value = standalone.strip()
        parser_fields = (
            "standalone_request", "intent", "parameters",
            "missing_information", "is_follow_up",
        )

        def mentioned_parser_fields(text: str) -> set[str]:
            lowered = text.lower()
            return {
                field for field in parser_fields
                if re.search(rf"(?<![a-z0-9_]){re.escape(field)}(?![a-z0-9_])", lowered)
            }

        def requests_json_output(text: str) -> bool:
            lowered = text.lower()
            patterns = (
                r"(?:输出|返回|生成|提供|导出|回复|响应)\s*(?:为|成|一个|一份|：|:)?\s*json(?:\s*(?:格式|对象|文件))?",
                r"(?:用|以|按|采用)\s*json(?:\s*(?:格式|对象))?\s*(?:输出|返回|生成|提供|导出|回复|响应)",
                r"json\s*(?:格式|对象|文件)?\s*(?:输出|返回|生成|提供|导出|回复|响应)",
                r"(?:output|return|respond|response)\b[^。.!！?\n]{0,40}\bjson\b",
                r"\bjson\b\s*(?:format|object|file|output|response)\b",
            )
            return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)

        original_fields = mentioned_parser_fields(original_message)
        original_requests_json = requests_json_output(original_message)

        def leaked_parser_instruction(segment: str) -> bool:
            segment_fields = mentioned_parser_fields(segment)
            same_explicit_field_group = (
                len(original_fields) >= 2 and segment_fields <= original_fields
            )
            if len(segment_fields) >= 2 and not same_explicit_field_group:
                return True
            return requests_json_output(segment) and not original_requests_json

        segments = re.split(r"(?<=[。！？.!?])\s*|\n+", value)
        value = "".join(
            segment for segment in segments
            if segment and not leaked_parser_instruction(segment)
        ).strip()
        return value or original_message.strip()

    def _json_object(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("意图解析模型未返回 JSON 对象")
        value = json.loads(text[start:end + 1])
        if not isinstance(value, dict):
            raise RuntimeError("意图解析结果必须是 JSON 对象")
        return value

    def _normalize_parameters(self, parameters: dict[str, Any], standalone_request: str = "") -> dict[str, Any]:
        normalized = dict(parameters)

        def first(*names: str) -> Any:
            return next((parameters[name] for name in names if parameters.get(name) not in (None, "")), None)

        city = first("city", "location", "place", "region", "城市", "地区", "地点")
        if city is not None:
            normalized["city"] = city
        output_format = first("format", "output_format", "file_format", "输出格式")
        if output_format is not None:
            normalized["format"] = output_format
        raw_day = first("day", "time", "relative_date", "日期", "时间")
        day_text = str(raw_day or standalone_request)
        if "后天" in day_text:
            normalized["day"] = "day_after_tomorrow"
        elif "明天" in day_text:
            normalized["day"] = "tomorrow"
        elif "今天" in day_text:
            normalized["day"] = "today"
        return normalized

    async def solve_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        model_config_id: str,
        tools: list[dict[str, Any]],
        invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_steps: int = 8,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        if not model_config_id or model_config_id == "deterministic":
            result = self._deterministic_response(prompt)
            if on_delta:
                try:
                    chunk_size = max(1, min(int(os.getenv("APP_DETERMINISTIC_STREAM_CHARS", "8")), 80))
                except ValueError:
                    chunk_size = 8
                try:
                    delay_seconds = max(
                        0.0,
                        min(float(os.getenv("APP_DETERMINISTIC_STREAM_DELAY_MS", "20")) / 1000, 0.5),
                    )
                except ValueError:
                    delay_seconds = 0.02
                for offset in range(0, len(result), chunk_size):
                    pending = on_delta(result[offset : offset + chunk_size])
                    if inspect.isawaitable(pending):
                        await pending
                    if delay_seconds and offset + chunk_size < len(result):
                        await asyncio.sleep(delay_seconds)
            return result
        row = db.query_one("SELECT * FROM model_configs WHERE id = ? AND enabled = 1", (model_config_id,))
        if not row:
            raise RuntimeError(f"模型配置不存在或未启用: {model_config_id}")
        require_outbound_network("在线模型调用")
        base_url = self._validated_base_url(row)
        api_key = self._api_key(row)
        config = db.json_loads(row.get("config_json"), {})
        name_map: dict[str, str] = {}
        api_tools = []
        for item in tools:
            raw_name = f"{item['server_id']}__{item['name']}"
            base_name = re.sub(r"[^A-Za-z0-9_-]", "_", raw_name)
            digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:10]
            safe_name = f"{base_name[:53]}_{digest}"
            if safe_name in name_map and name_map[safe_name] != raw_name:
                raise RuntimeError("工具名称规范化后发生冲突，请调整 MCP Server 或工具 ID")
            name_map[safe_name] = raw_name
            api_tools.append({"type": "function", "function": {"name": safe_name, "description": item.get("description", ""), "parameters": item.get("input_schema") or {"type": "object", "properties": {}}}})
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend(
            {"role": item["role"], "content": item["content"]}
            for item in (history or [])[-20:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        )
        messages.append({"role": "user", "content": prompt})
        timeout = float(config.get("timeout", 90))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(max(1, min(max_steps, 20))):
                payload: dict[str, Any] = {"model": row["model"], "messages": messages, "temperature": config.get("temperature", 0.2)}
                if api_tools:
                    payload["tools"] = api_tools
                    payload["tool_choice"] = "auto"
                message = None
                last_status_error: httpx.HTTPStatusError | None = None
                for attempt in range(3):
                    try:
                        message = await self._stream_completion(
                            client,
                            f"{base_url}/chat/completions",
                            payload,
                            {"Authorization": f"Bearer {api_key}"},
                            on_delta,
                        )
                        break
                    except httpx.HTTPStatusError as exc:
                        retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                        if not retryable:
                            raise
                        last_status_error = exc
                        if attempt < 2:
                            await asyncio.sleep(0.6 * (2 ** attempt))
                if message is None:
                    fallback = await client.post(
                        f"{base_url}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    try:
                        fallback.raise_for_status()
                    except httpx.HTTPStatusError:
                        if last_status_error:
                            raise last_status_error
                        raise
                    message = fallback.json()["choices"][0]["message"]
                    fallback_text = str(message.get("content") or "")
                    if fallback_text and on_delta:
                        pending = on_delta(fallback_text)
                        if inspect.isawaitable(pending):
                            await pending
                messages.append(message)
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    return str(message.get("content") or "")
                for call in tool_calls:
                    function = call.get("function", {})
                    mapped = name_map.get(function.get("name", ""))
                    if not mapped:
                        raise RuntimeError("模型请求了未授权工具，已停止本次执行")
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError("模型提供的工具参数不是有效 JSON，已停止本次执行") from exc
                    if not isinstance(arguments, dict):
                        raise RuntimeError("模型提供的工具参数必须是 JSON 对象，已停止本次执行")
                    # A failed tool result is not evidence that the task was
                    # completed.  Propagate the error so the runtime can mark
                    # the current node and task failed instead of allowing the
                    # model to improvise a final answer from an error string.
                    result = await invoke(mapped, arguments)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": db.json_dumps(result)[:50000]})
        raise RuntimeError(f"模型工具调用超过最大步数 {max_steps}")

    @staticmethod
    def _deterministic_response(prompt: str) -> str:
        """Create a transparent offline response while preserving supplied source text.

        The deterministic adapter is an offline fallback rather than a reasoning
        model. Preserving the bounded attachment block keeps generated files tied
        to the material the user supplied.
        """
        attachment_marker = "用户附件内容："
        attachment = ""
        if attachment_marker in prompt:
            attachment = prompt.split(attachment_marker, 1)[1]
            attachment = attachment.split("\n\n当前执行计划", 1)[0].strip()
        if attachment:
            return (
                "以下内容根据用户上传的资料整理（离线确定性模型未做事实扩写）：\n\n"
                + attachment
            )
        request = prompt.split("\n\n请遵循以下已匹配 Skill：", 1)[0].strip()
        if request.startswith("用户最新原话：") and "结合上下文还原后的当前独立任务：" in request:
            request = request.split("结合上下文还原后的当前独立任务：", 1)[1].strip()
        return (
            "离线确定性模型已接收当前任务，但不会虚构分析结论。"
            + (f"\n\n当前目标：{request}" if request else "")
            + "\n\n如需完整推理与内容创作，请在模型设置中选择已配置的在线模型。"
        )

    async def _stream_completion(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_delta: Callable[[str], Awaitable[None] | None] | None,
    ) -> dict[str, Any]:
        """Read OpenAI-compatible SSE while forwarding text deltas and assembling tool calls."""
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        plain_response_lines: list[str] = []
        saw_sse_event = False
        async with client.stream("POST", url, json={**payload, "stream": True}, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    plain_response_lines.append(line)
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                saw_sse_event = True
                event = json.loads(raw)
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if isinstance(text, str) and text:
                    content_parts.append(text)
                    if on_delta:
                        pending = on_delta(text)
                        if inspect.isawaitable(pending):
                            await pending
                for call in delta.get("tool_calls") or []:
                    index = int(call.get("index", 0))
                    current = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if call.get("id"):
                        current["id"] += str(call["id"])
                    function = call.get("function") or {}
                    current["function"]["name"] += str(function.get("name") or "")
                    current["function"]["arguments"] += str(function.get("arguments") or "")
        if not saw_sse_event and plain_response_lines:
            raw_response = "\n".join(plain_response_lines).strip()
            try:
                data = json.loads(raw_response)
            except json.JSONDecodeError as exc:
                raise RuntimeError("模型接口未返回有效的流式事件或 JSON 响应") from exc
            choices = data.get("choices") if isinstance(data, dict) else None
            message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
            if not isinstance(message, dict):
                raise RuntimeError("模型接口返回的 JSON 缺少 choices[0].message")
            message = dict(message)
            message.setdefault("role", "assistant")
            text = message.get("content")
            if isinstance(text, str) and text and on_delta:
                pending = on_delta(text)
                if inspect.isawaitable(pending):
                    await pending
            return message
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        return message

    def _api_key(self, row: dict[str, Any]) -> str:
        encrypted = row.get("api_key_ciphertext") or ""
        if encrypted:
            return secret_store.decrypt(encrypted)
        api_key_env = row.get("api_key_env") or "OPENAI_API_KEY"
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise RuntimeError(f"缺少模型密钥：请配置环境变量 {api_key_env}，或在模型设置中直接填写 API Key")
        return api_key

    @staticmethod
    def _validated_base_url(row: dict[str, Any]) -> str:
        base_url = (row.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        return validate_outbound_http_url(
            base_url,
            capability="模型 API",
            allowlist_env="APP_MODEL_HOST_ALLOWLIST",
            allow_non_public_when_allowlisted=True,
            require_https_unless_explicitly_allowlisted=True,
            allow_query=False,
        )

    def _deterministic_summary(self, prompt: str, context: dict[str, Any]) -> str:
        lines = ["离线确定性模型已处理本次摘要请求。"]
        if context.get("selected_skills"):
            lines.append("已选择 Skill：" + "、".join(context["selected_skills"]))
        if context.get("tool_count") is not None:
            lines.append(f"本次任务调用工具 {context['tool_count']} 次。")
        return "\n".join(lines)

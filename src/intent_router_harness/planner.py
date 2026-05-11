from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from intent_router_harness.contracts import (
    AssistantTraceEvent,
    PlannerOutput,
    RouterMessageRequest,
    TaskRuntimeState,
)
from intent_router_harness.llm import LLMClient, LLMRequestError
from intent_router_harness.runtime import PromptHarness
from intent_router_harness.skills import SkillDocument
from intent_router_harness.trace import emit_trace
from intent_router_harness.workflow import is_workflow_tool_reference_body

logger = logging.getLogger(__name__)

ASSISTANT_STATUS_VALUES = [
    "running",
    "waiting_user_input",
    "ready_for_dispatch",
    "waiting_assistant_completion",
    "completed",
    "cancelled",
    "failed",
]

_LLM_PRIVATE_CONTEXT_KEYS = {
    "sessionid",
    "session_id",
    "session",
    "agentsessionid",
    "agent_session_id",
    "agentsession",
    "custid",
    "cust_id",
    "custno",
    "cust_no",
    "userid",
    "user_id",
}


class PlannerError(RuntimeError):
    """Raised when planner output cannot be produced or validated."""


class MessagePlanner(Protocol):
    """Planner boundary for message requests."""

    def plan_message(
        self,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
    ) -> PlannerOutput:
        """Return a structured planner output."""


@dataclass(frozen=True, slots=True)
class _PlannerPrompt:
    """Prompt object used by the assistant runtime."""

    phase: str
    system: str
    human: str
    agent_contexts: tuple[str, ...] = ()
    metadata_skills: tuple[str, ...] = ()
    loaded_skills: tuple[str, ...] = ()
    loaded_references: tuple[str, ...] = ()
    trace_events: tuple[dict[str, Any], ...] = ()

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.human},
        ]


class LLMMessagePlanner:
    """Two-stage planner: recognize tasks first, then fill the current task only."""

    def __init__(
        self,
        *,
        harness: PromptHarness,
        llm_client: LLMClient,
        max_tokens: int = 1200,
    ) -> None:
        self.harness = harness
        self.llm_client = llm_client
        self.max_tokens = max_tokens

    def plan_message(
        self,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
    ) -> PlannerOutput:
        """Recognize task queue, then fill slots for the current task only."""
        include_trace = request.debugTrace
        logger.info(
            "llm.plan.start session_id=%s execution_mode=%s text=%s task_slot_memory=%s current_task=%s",
            request.sessionId,
            request.executionMode,
            _truncate_for_log(request.txt, 300),
            task_state.slot_memory,
            task_state.current_task.model_dump(mode="json") if task_state.current_task else None,
        )
        active_context = task_state.active_context if isinstance(task_state.active_context, dict) else {}
        requested_reference_ids = tuple(_string_list(active_context.get("reference_ids")))
        variables = {
            "message": request.txt,
            "execution_mode": request.executionMode,
            "task_state_json": _llm_context_json(task_state.model_dump(mode="json", exclude_none=True)),
            "recommend_task_json": _llm_context_json(request.recommendTask),
            "recent_messages_json": "[]",
            "config_variables_json": json.dumps(
                [item.model_dump(mode="json") for item in request.config_variables],
                ensure_ascii=False,
            ),
            "planner_output_schema_json": _planner_output_schema_json(),
        }
        trace_events: list[dict[str, Any]] = []

        if _has_active_task(task_state):
            task_list = list(task_state.task_list)
            current_task = task_state.current_task or _first_active_task(task_list)
        else:
            intent_prompt = self._render_intent_prompt(request=request, variables=variables)
            _record_prompt_trace(
                request=request,
                prompt=intent_prompt,
                include_trace=include_trace,
                trace_events=trace_events,
                title="意图识别提示词加载",
            )
            raw_response, content = self._call_llm(request, intent_prompt)
            _record_raw_response_trace(
                request=request,
                raw_response=raw_response,
                content=content,
                include_trace=include_trace,
                trace_events=trace_events,
            )
            task_list = self._parse_intent_tasks(request, content)
            current_task = _first_active_task(task_list)

        if current_task is None:
            plan = PlannerOutput(
                mode="failed",
                status="failed",
                completion_state=2,
                completion_reason="router_intent_not_recognized",
                message="未识别出可处理的业务意图",
                output={},
            )
            return plan

        skill = self._skill_for_intent(request, current_task.intent_code)
        requested_reference_ids = tuple(
            _merge_strings((*_default_slot_reference_ids(skill), *requested_reference_ids))
        )
        prompt = self._render_slot_prompt(
            request=request,
            variables=variables,
            task_list=task_list,
            current_task=current_task,
            skill=skill,
            requested_reference_ids=requested_reference_ids,
        )
        _record_prompt_trace(
            request=request,
            prompt=prompt,
            include_trace=include_trace,
            trace_events=trace_events,
            title="当前任务提槽提示词加载",
        )
        raw_response, content = self._call_llm(request, prompt)
        _record_raw_response_trace(
            request=request,
            raw_response=raw_response,
            content=content,
            include_trace=include_trace,
            trace_events=trace_events,
        )
        slot_payload = self._parse_slot_payload(request, content)
        plan = self._build_plan_from_slot_payload(
            request=request,
            task_state=task_state,
            task_list=task_list,
            current_task=current_task,
            skill=skill,
            slot_payload=slot_payload,
        )
        payload = plan.model_dump(mode="json")

        if plan.requested_references:
            requested_reference_ids = tuple(
                _merge_strings((*requested_reference_ids, *tuple(plan.requested_references)))
            )
            logger.info(
                "llm.plan.reference_request session_id=%s requested_references=%s",
                request.sessionId,
                list(requested_reference_ids),
            )
            if include_trace:
                event = AssistantTraceEvent(
                    stage="reference_request_received",
                    title="LLM请求加载Reference",
                    summary=f"requested_references={list(requested_reference_ids)}",
                    data={
                        "requested_references": list(requested_reference_ids),
                        "first_pass_completion_reason": plan.completion_reason,
                    },
                )
                trace_events.append(event.model_dump(mode="json"))
                emit_trace(event)
            prompt = self._render_slot_prompt(
                request=request,
                variables=variables,
                task_list=task_list,
                current_task=current_task,
                skill=skill,
                requested_reference_ids=requested_reference_ids,
            )
            _record_prompt_trace(
                request=request,
                prompt=prompt,
                include_trace=include_trace,
                trace_events=trace_events,
                title="Reference补充后提示词加载",
            )
            raw_response, content = self._call_llm(request, prompt)
            _record_raw_response_trace(
                request=request,
                raw_response=raw_response,
                content=content,
                include_trace=include_trace,
                trace_events=trace_events,
            )
            slot_payload = self._parse_slot_payload(request, content)
            plan = self._build_plan_from_slot_payload(
                request=request,
                task_state=task_state,
                task_list=task_list,
                current_task=current_task,
                skill=skill,
                slot_payload=slot_payload,
            )
            payload = plan.model_dump(mode="json")

        logger.info(
            "llm.plan.validated session_id=%s mode=%s status=%s intent_code=%s completion_reason=%s slot_memory=%s task_count=%d output=%s",
            request.sessionId,
            plan.mode,
            plan.status,
            plan.intent_code,
            plan.completion_reason,
            plan.slot_memory,
            len(plan.task_list),
            plan.output,
        )
        logger.info(
            "core.trace step=llm_analysis session_id=%s intent_code=%s mode=%s status=%s completion_reason=%s slot_memory=%s current_task=%s message=%s loaded_skills=%s loaded_references=%s",
            request.sessionId,
            _effective_intent_code(plan),
            plan.mode,
            plan.status,
            plan.completion_reason,
            plan.slot_memory,
            _task_for_log(plan.current_task),
            _truncate_for_log(plan.message, 500),
            list(prompt.loaded_skills),
            list(prompt.loaded_references),
        )
        diagnostics = dict(plan.diagnostics)
        diagnostics["_router_context"] = {
            "agent_contexts": list(prompt.agent_contexts),
            "metadata_skills": list(prompt.metadata_skills),
            "skill_names": list(prompt.loaded_skills),
            "reference_ids": list(prompt.loaded_references),
            **self._all_skill_context_maps(prompt),
        }
        if include_trace:
            event = AssistantTraceEvent(
                stage="llm_analysis",
                title="LLM结构化分析",
                summary=(
                    f"intent_code={_effective_intent_code(plan)}，"
                    f"status={plan.status}，reason={plan.completion_reason}"
                ),
                data={
                    "mode": plan.mode,
                    "status": plan.status,
                    "completion_reason": plan.completion_reason,
                    "intent_code": _effective_intent_code(plan),
                    "slot_memory": plan.slot_memory,
                    "task_list": [task.model_dump(mode="json") for task in plan.task_list],
                    "current_task": plan.current_task.model_dump(mode="json")
                    if plan.current_task
                    else None,
                    "message": plan.message,
                    "output": plan.output,
                    "parsed_json": payload,
                    "router_context": diagnostics["_router_context"],
                },
            )
            trace_events.append(event.model_dump(mode="json"))
            emit_trace(event)
            diagnostics["_router_trace_events"] = trace_events
        plan = plan.model_copy(update={"diagnostics": diagnostics}, deep=True)
        return plan

    def _render_intent_prompt(
        self,
        *,
        request: RouterMessageRequest,
        variables: dict[str, Any],
    ) -> _PlannerPrompt:
        metadata_skills = self._metadata_skills()
        skill_lines = [
            f"- name={skill.name}; intent_codes={list(skill.intent_codes)}; description={skill.description}"
            for skill in metadata_skills
        ]
        agent_context, agent_trace_events = self._agent_context_events()
        system = "\n\n".join(
            part
            for part in [
                "\n".join(
                    [
                        "你负责做意图识别和多意图拆分。",
                        "只允许依据可用 Skill 摘要中的 name、description 和 intent_codes 做判断。",
                        "不要加载、复述或依赖任何 Skill 正文。",
                        "不要做提槽，不要输出 slot_memory，不要判断 ready/waiting。",
                        "一个 task 只能承载一个 intent_code；一个用户请求包含多个独立业务动作时，按用户表达顺序拆成多个 task。",
                        "每个 task 必须包含 taskId、intent_code、title、source_text。source_text 是该任务对应的原始用户片段。",
                        "只返回 JSON：{\"tasks\":[...],\"reason\":\"...\"}。",
                        "输出必须是原始 JSON 对象文本，第一个字符必须是 {，最后一个字符必须是 }。",
                        "禁止使用 Markdown、代码块、```json、解释性文字或任何 JSON 外层包装。",
                        "不要输出思考过程，不要逐步展开分析，直接给出最终 JSON。",
                    ]
                ),
                agent_context,
                "## 可用 Skill 摘要\n" + "\n".join(skill_lines),
            ]
            if part.strip()
        )
        human = "\n\n".join(
            [
                f"用户消息：\n{variables['message']}",
                f"执行模式：\n{variables['execution_mode']}",
                f"任务运行态 JSON：\n{variables['task_state_json']}",
                f"推荐任务 JSON：\n{variables['recommend_task_json']}",
                f"最近展示上下文 JSON：\n{variables['recent_messages_json']}",
                f"配置变量 JSON：\n{variables['config_variables_json']}",
            ]
        )
        return _PlannerPrompt(
            phase="intent_recognition",
            system=system,
            human=human,
            agent_contexts=tuple(str(context.path) for context in self.harness.agent_contexts),
            metadata_skills=tuple(skill.name for skill in metadata_skills),
            trace_events=tuple(agent_trace_events),
        )

    def _render_slot_prompt(
        self,
        *,
        request: RouterMessageRequest,
        variables: dict[str, Any],
        task_list: list[Any],
        current_task: Any,
        skill: SkillDocument,
        requested_reference_ids: tuple[str, ...],
    ) -> _PlannerPrompt:
        agent_context, agent_trace_events = self._agent_context_events()
        prompt_references = [
            reference
            for reference in skill.references
            if not is_workflow_tool_reference_body(reference.body)
        ]
        available_references = {reference.id: reference for reference in prompt_references}
        loaded_references = [
            reference
            for reference in prompt_references
            if reference.id in set(requested_reference_ids)
        ]
        missing = [reference_id for reference_id in requested_reference_ids if reference_id not in available_references]
        if missing:
            raise PlannerError(f"requested references are not exposed by current skill: {missing}")
        max_skill_chars = self.harness.spec.max_skill_body_chars
        max_ref_chars = self.harness.spec.max_reference_body_chars
        rendered_skill_body = _truncate(skill.body, max_skill_chars)
        reference_summary = "\n".join(
            f"- {reference.id}: {reference.purpose}" for reference in prompt_references
        )
        reference_bodies = "\n\n".join(
            f"### {reference.id}\n{_truncate(reference.body, max_ref_chars)}"
            for reference in loaded_references
        )
        trace_events = [
            *agent_trace_events,
            {
                "stage": "spec_progressive_load",
                "title": "Skill渐进式加载",
                "summary": f"当前任务加载 skill={skill.name}",
                "data": {
                    "metadata_skills": [item.name for item in self._metadata_skills()],
                    "loaded_skill_bodies": [skill.name],
                    "available_references": sorted(available_references),
                    "loaded_references": [reference.id for reference in loaded_references],
                },
            },
            {
                "stage": "skill_body_loaded",
                "title": "Skill正文加载",
                "summary": f"{skill.name} 已加载到当前任务提槽 prompt",
                "data": {
                    "skill": skill.name,
                    "description": skill.description,
                    "path": str(skill.path),
                    "body_chars": len(skill.body),
                    "truncated_to": max_skill_chars,
                    "body": rendered_skill_body,
                },
            },
        ]
        for reference in loaded_references:
            trace_events.append(
                {
                    "stage": "reference_body_loaded",
                    "title": "Reference正文加载",
                    "summary": f"{reference.id} 已加载到当前任务提槽 prompt",
                    "data": {
                        "reference_id": reference.id,
                        "skill": skill.name,
                        "path": str(reference.path),
                        "body_chars": len(reference.body),
                        "truncated_to": max_ref_chars,
                        "body": _truncate(reference.body, max_ref_chars),
                    },
                }
            )
        system_parts = [
            "你负责对当前任务做补槽。只处理 current_task，不要修改、提槽或推进其他任务。",
            "只返回 JSON，字段允许：slot_memory、message、requested_references、diagnostics。",
            "输出必须是原始 JSON 对象文本，第一个字符必须是 {，最后一个字符必须是 }。",
            "禁止使用 Markdown、代码块、```json、解释性文字或任何 JSON 外层包装。",
            "槽位齐全时输出 slot_memory 并让任务状态为 ready_for_dispatch，无需输出 workflow_request。",
            "slot_memory 只能包含当前 skill 定义的当前任务槽位；不要输出 task_list，不要输出其他任务的槽位。",
            "数值类槽位按当前 skill 要求保存。只合并最新消息或 current_task.source_text 中有依据的新槽位。",
            "如果确实需要已暴露 reference 才能完成判断，返回 requested_references。",
            agent_context,
            f"## 当前 Skill 摘要\n- name={skill.name}\n- intent_codes={list(skill.intent_codes)}\n- required_slots={list(skill.required_slots)}\n- description={skill.description}",
            f"## 当前 Skill 正文\n{rendered_skill_body}",
        ]
        if reference_summary:
            system_parts.append("## 可用 Reference 摘要\n" + reference_summary)
        if reference_bodies:
            system_parts.append("## 已加载 Reference 正文\n" + reference_bodies)
        system_parts.append(
            "\n".join(
                [
                    "## 最终输出硬约束",
                    "不要输出思考过程，不要逐步展开分析；即使 Skill 正文要求逐步分析，也只能在内部完成判断。",
                    "必须直接输出一个原始 JSON 对象，首字符为 {，尾字符为 }。",
                    "禁止输出 Markdown、```json 代码块、自然语言解释或任何 JSON 外文本。",
                    "如果没有可补充槽位，也必须输出 JSON，例如 {\"slot_memory\":{},\"message\":\"请提供缺失信息\"}。",
                ]
            )
        )
        human = "\n\n".join(
            [
                "/no_think",
                f"用户最新消息：\n{variables['message']}",
                f"执行模式：\n{variables['execution_mode']}",
                f"当前任务 JSON：\n{_llm_context_json(_task_json(current_task))}",
                f"完整任务队列 JSON（只读，禁止修改非当前任务）：\n{_llm_context_json([_task_json(task) for task in task_list])}",
                f"当前任务已知槽位 JSON：\n{_llm_context_json(getattr(current_task, 'slot_memory', {}))}",
                f"配置变量 JSON（可用于 workflow_request 参数组装）：\n{variables['config_variables_json']}",
                f"任务运行态 JSON：\n{variables['task_state_json']}",
            ]
        )
        return _PlannerPrompt(
            phase="slot_filling",
            system="\n\n".join(part for part in system_parts if part.strip()),
            human=human,
            agent_contexts=tuple(str(context.path) for context in self.harness.agent_contexts),
            metadata_skills=tuple(skill.name for skill in self._metadata_skills()),
            loaded_skills=(skill.name,),
            loaded_references=tuple(reference.id for reference in loaded_references),
            trace_events=tuple(trace_events),
        )

    def _metadata_skills(self) -> list[SkillDocument]:
        return [
            skill
            for name in self.harness.skills.names()
            if (skill := self.harness.skills.get(name)) is not None and skill.description
        ]

    def _agent_context_events(self) -> tuple[str, list[dict[str, Any]]]:
        if not self.harness.agent_contexts:
            return "", []
        lines = ["## Agent 根指令"]
        event_data: list[dict[str, Any]] = []
        for context in self.harness.agent_contexts:
            lines.extend([f"### {context.path.name}", context.body])
            event_data.append(
                {"path": str(context.path), "body_chars": len(context.body), "body": context.body}
            )
        return "\n".join(lines), [
            {
                "stage": "agent_context_loaded",
                "title": "Agent根指令加载",
                "summary": f"加载 {len(self.harness.agent_contexts)} 个 agent context",
                "data": {"agent_contexts": event_data},
            }
        ]

    def _parse_intent_tasks(self, request: RouterMessageRequest, content: str) -> list[Any]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PlannerError(f"intent recognition output is not JSON: {exc}") from exc
        raw_tasks = payload.get("tasks", payload.get("task_list", []))
        if not raw_tasks and payload.get("skill_names"):
            raw_tasks = []
            for skill_name in _string_list(payload.get("skill_names")):
                skill = self.harness.skills.get(skill_name)
                if skill is None or not skill.intent_codes:
                    continue
                raw_tasks.append(
                    {
                        "intent_code": skill.intent_codes[0],
                        "title": skill.description or skill.name,
                        "source_text": request.txt,
                    }
                )
        if not isinstance(raw_tasks, list):
            raise PlannerError("intent recognition output tasks must be a list")
        allowed_intents = self._declared_intents()
        tasks = []
        for index, item in enumerate(raw_tasks, start=1):
            if not isinstance(item, dict):
                continue
            intent_code = str(item.get("intent_code") or "").strip()
            if intent_code not in allowed_intents:
                raise PlannerError(
                    f"intent recognition emitted undeclared intent_code: {intent_code!r}"
                )
            task_id = str(item.get("taskId") or f"task_{index:03d}").strip()
            title = str(item.get("title") or allowed_intents[intent_code].description).strip()
            source_text = str(item.get("source_text") or request.txt).strip()
            tasks.append(
                {
                    "taskId": task_id,
                    "intent_code": intent_code,
                    "status": "waiting_user_input",
                    "title": title,
                    "slot_memory": {},
                    "output": {},
                    "source_text": source_text,
                }
            )
        try:
            return [PlannerOutput.model_validate(
                {
                    "mode": "single_task",
                    "status": "waiting_user_input",
                    "completion_reason": "router_waiting_user_input",
                    "task_list": tasks,
                }
            ).task_list[index] for index in range(len(tasks))]
        except ValidationError as exc:
            raise PlannerError(f"intent recognition tasks failed schema validation: {exc}") from exc

    def _parse_slot_payload(self, request: RouterMessageRequest, content: str) -> dict[str, Any]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PlannerError(f"slot filling output is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise PlannerError("slot filling output must be a JSON object")
        logger.info(
            "llm.slot.parsed_json session_id=%s payload=%s",
            request.sessionId,
            _truncate_for_log(json.dumps(payload, ensure_ascii=False), 4000),
        )
        return payload

    def _build_plan_from_slot_payload(
        self,
        *,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
        task_list: list[Any],
        current_task: Any,
        skill: SkillDocument,
        slot_payload: dict[str, Any],
    ) -> PlannerOutput:
        slot_delta = slot_payload.get("slot_memory", slot_payload.get("slots", {}))
        if not isinstance(slot_delta, dict):
            slot_delta = {}
        self._validate_slot_payload_scope(
            request=request,
            task_state=task_state,
            current_task=current_task,
            skill=skill,
            slot_payload=slot_payload,
        )
        current_slots = dict(getattr(current_task, "slot_memory", {}) or {})
        current_slots.update(slot_delta)
        required_slots = _required_slots(skill)
        missing_slots = [slot for slot in required_slots if not _slot_has_value(current_slots.get(slot))]
        status = "ready_for_dispatch" if not missing_slots else "waiting_user_input"
        completion_reason = (
            "router_ready_for_dispatch"
            if status == "ready_for_dispatch"
            else "router_waiting_user_input"
        )
        message = "" if status == "ready_for_dispatch" else _missing_slots_message(skill, missing_slots)
        workflow_request: dict[str, Any] = {}
        updated_current = current_task.model_copy(
            update={"slot_memory": current_slots, "status": status, "workflow_request": workflow_request},
            deep=True,
        )
        updated_tasks = [
            updated_current if task.taskId == updated_current.taskId else task
            for task in task_list
        ]
        mode = "multi_task" if len(updated_tasks) > 1 else "slot_filling"
        requested_references = _string_list(slot_payload.get("requested_references"))
        return PlannerOutput(
            mode=mode,
            status=status,
            completion_state=0,
            completion_reason=completion_reason,
            intent_code=updated_current.intent_code,
            recognition={"intent_code": updated_current.intent_code},
            slot_memory=current_slots,
            task_list=updated_tasks,
            current_task=updated_current,
            requested_references=requested_references,
            message=message,
            output={},
            workflow_request=workflow_request,
            diagnostics={
                "slot_payload": slot_payload,
                "missing_slots": missing_slots,
            },
        )

    def _validate_slot_payload_scope(
        self,
        *,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
        current_task: Any,
        skill: SkillDocument,
        slot_payload: dict[str, Any],
    ) -> None:
        allowed_intents = set(skill.intent_codes)
        existing_task_intents = _existing_task_intents(task_state)
        emitted: list[tuple[str, str, str | None]] = []
        if slot_payload.get("intent_code"):
            emitted.append(("intent_code", str(slot_payload["intent_code"]), None))
        recognition = slot_payload.get("recognition")
        if isinstance(recognition, dict) and recognition.get("intent_code"):
            emitted.append(("recognition.intent_code", str(recognition["intent_code"]), None))
        for index, item in enumerate(slot_payload.get("task_list") or []):
            if isinstance(item, dict) and item.get("intent_code"):
                emitted.append(
                    (
                        f"task_list[{index}].intent_code",
                        str(item["intent_code"]),
                        str(item.get("taskId") or ""),
                    )
                )
        raw_current = slot_payload.get("current_task")
        if isinstance(raw_current, dict) and raw_current.get("intent_code"):
            emitted.append(
                (
                    "current_task.intent_code",
                    str(raw_current["intent_code"]),
                    str(raw_current.get("taskId") or ""),
                )
            )
        invalid = [
            {"field": field, "intent_code": intent_code}
            for field, intent_code, task_id in emitted
            if intent_code not in allowed_intents
            and (not task_id or existing_task_intents.get(task_id) != intent_code)
        ]
        if invalid:
            raise PlannerError(
                "LLM planner emitted intent_code not declared by loaded skills: "
                f"session_id={request.sessionId} invalid={invalid} allowed={sorted(allowed_intents)}"
            )
        raw_current_task = slot_payload.get("current_task")
        if isinstance(raw_current_task, dict):
            task_id = str(raw_current_task.get("taskId") or "")
            if task_id and task_id != current_task.taskId:
                raise PlannerError(
                    "LLM slot filler attempted to modify a non-current task: "
                    f"session_id={request.sessionId} task_id={task_id} current_task={current_task.taskId}"
                )

    def _skill_for_intent(self, request: RouterMessageRequest, intent_code: str) -> SkillDocument:
        matches = [
            skill
            for skill in self._metadata_skills()
            if intent_code in skill.intent_codes
        ]
        if not matches:
            raise PlannerError(
                f"no skill declares intent_code={intent_code!r}: session_id={request.sessionId}"
            )
        return matches[0]

    def _declared_intents(self) -> dict[str, SkillDocument]:
        result: dict[str, SkillDocument] = {}
        for skill in self._metadata_skills():
            for intent_code in skill.intent_codes:
                result[intent_code] = skill
        return result

    def _all_skill_context_maps(self, prompt: Any) -> dict[str, dict[str, list[str]]]:
        skill_intent_map: dict[str, list[str]] = {}
        intent_skill_map: dict[str, list[str]] = {}
        reference_skill_map: dict[str, list[str]] = {}
        loaded_references = set(getattr(prompt, "loaded_references", ()))
        for skill in self._metadata_skills():
            skill_intent_map[skill.name] = list(skill.intent_codes)
            for intent_code in skill.intent_codes:
                intent_skill_map.setdefault(intent_code, []).append(skill.name)
            for reference in skill.references:
                if reference.id in loaded_references:
                    reference_skill_map.setdefault(reference.id, []).append(skill.name)
        return {
            "skill_intent_map": skill_intent_map,
            "intent_skill_map": intent_skill_map,
            "reference_skill_map": reference_skill_map,
        }

    def _call_llm(self, request: RouterMessageRequest, prompt):
        logger.debug(
            "llm.plan.prompt_system session_id=%s content=%s",
            request.sessionId,
            _truncate_for_log(prompt.system, 12000),
        )
        logger.debug(
            "llm.plan.prompt_human session_id=%s content=%s",
            request.sessionId,
            _truncate_for_log(prompt.human, 8000),
        )
        try:
            raw_response = self.llm_client.chat(prompt.messages(), max_tokens=self.max_tokens)
            content = str(raw_response["choices"][0]["message"]["content"]).strip()
            return raw_response, content
        except (KeyError, IndexError, TypeError, LLMRequestError) as exc:
            raise PlannerError(f"LLM planner request failed: {exc}") from exc


def _planner_output_schema_json() -> str:
    schema = {
        "required": ["mode", "status", "completion_reason"],
        "status_values": ASSISTANT_STATUS_VALUES,
        "rules": [
            "PlannerOutput.status、task_list 每个元素的 status、current_task.status 只能使用 status_values 中的值。",
            "不要输出 pending、queued、todo、incomplete、input_required 等非标准状态。",
            "不要把 enum、required、fields、rules、description 等 schema 辅助键复制到输出 JSON。",
            "如果缺少必填槽位，使用 status=waiting_user_input 和 completion_reason=router_waiting_user_input。",
            "如果 router_only 模式下必填槽位齐全，使用 status=ready_for_dispatch 和 completion_reason=router_ready_for_dispatch。",
            "只能使用已加载 skill 中声明的标准 intent_code，不要编造展示名或泛化标签。",
            "当 task runtime state 中存在等待中的活跃任务时，将短回复优先解释为该任务的槽位值，并保留已有 slot_memory。",
            "补槽时必须整体解析最新消息；如果同一条消息明确提供多个当前任务缺失槽位，应一次性写入所有有依据的槽位。",
            "slot_memory 的键必须来自当前已加载 skill 声明的 required_slots 或 skill 正文定义的槽位语义，不要自造业务键。",
            "当 task runtime state 中存在多个等待任务时，第一笔/第一次/第一个、第二笔/第二次/第二个等顺序表达应按 task_list 顺序定位任务并补充对应 slot_memory。",
            "recommendTask 只作为当前轮 router 上下文；只有用户明确选择全部、部分或指定推荐任务时，才基于推荐任务创建 task。",
            "如果用户未采纳推荐任务而表达其他诉求，不要把推荐任务写入 task_list。",
            "如果用户没有对推荐任务做出选择，recommendTask 不得影响后续 task runtime state。",
            "如果已加载 skill 暴露了可用 reference 且确实需要更多上下文，将 requested_references 设置为允许的 reference id，status=running，completion_reason=router_reference_required。",
            "不要请求未在可用 Reference 摘要中列出的 reference id。",
        ],
        "fields": {
            "mode": "single_task | multi_task | slot_filling | cancel | replan | failed",
            "status": {"enum": ASSISTANT_STATUS_VALUES},
            "completion_state": "0 表示处理中，1 表示需要助手确认，2 表示终态",
            "completion_reason": "稳定、机器可读的原因码",
            "intent_code": "已选择的业务意图代码，没有则为空",
            "recognition": {
                "intent_code": "已选择的业务意图代码",
            },
            "slot_memory": "包含稳定槽位键的对象",
            "task_list": [
                {
                    "taskId": "稳定任务 id",
                    "intent_code": "业务意图代码",
                    "status": {"enum": ASSISTANT_STATUS_VALUES},
                    "title": "简短展示标题",
                    "slot_memory": "对象",
                    "output": "对象",
                }
            ],
            "current_task": {
                "taskId": "与 task_list 中活跃任务一致的 taskId",
                "intent_code": "与 task_list 中活跃任务一致的 intent_code",
                "status": {"enum": ASSISTANT_STATUS_VALUES},
                "title": "与 task_list 中活跃任务一致的 title",
                "slot_memory": "对象",
                "output": "对象",
            },
            "graph": "null 或明确的多任务依赖图",
            "actions": "可选的图或 action-flow 操作",
            "requested_references": "最终规划前需要加载的可选 reference id 列表，必须来自允许列表",
            "message": "面向用户的消息",
            "output": "协议输出对象；不要在 output 内包含 slot_memory",
            "workflow_request": "(由 executor 层通过 function call 独立组装，planner 无需输出)",
            "diagnostics": "调试对象",
        },
    }
    return json.dumps(schema, ensure_ascii=False)


def _record_prompt_trace(
    *,
    request: RouterMessageRequest,
    prompt: Any,
    include_trace: bool,
    trace_events: list[dict[str, Any]],
    title: str,
) -> None:
    logger.info(
        "llm.plan.prompt_rendered session_id=%s phase=%s agent_contexts=%s metadata_skills=%s loaded_skills=%s loaded_references=%s system_chars=%d human_chars=%d",
        request.sessionId,
        prompt.phase,
        list(prompt.agent_contexts),
        list(prompt.metadata_skills),
        list(prompt.loaded_skills),
        list(prompt.loaded_references),
        len(prompt.system),
        len(prompt.human),
    )
    logger.info(
        "core.trace step=prompt_loaded session_id=%s phase=%s system_contains=stage_rules+agent_context+skill_context human_contains=user_message+task_runtime_state loaded_skills=%s loaded_references=%s system_chars=%d human_chars=%d",
        request.sessionId,
        prompt.phase,
        list(prompt.loaded_skills),
        list(prompt.loaded_references),
        len(prompt.system),
        len(prompt.human),
    )
    if not include_trace:
        return

    for raw_event in prompt.trace_events:
        event = AssistantTraceEvent.model_validate(raw_event)
        trace_events.append(event.model_dump(mode="json"))
        emit_trace(event)
    event = AssistantTraceEvent(
        stage="prompt_loaded",
        title=title,
        summary=(
            "system prompt 包含阶段规则、agent 根指令、skill 上下文；human prompt 包含用户消息和任务运行态"
        ),
        data={
            "phase": prompt.phase,
            "agent_contexts": list(prompt.agent_contexts),
            "metadata_skills": list(prompt.metadata_skills),
            "loaded_skills": list(prompt.loaded_skills),
            "loaded_references": list(prompt.loaded_references),
            "system_chars": len(prompt.system),
            "human_chars": len(prompt.human),
            "system_prompt": prompt.system,
            "human_prompt": prompt.human,
        },
    )
    trace_events.append(event.model_dump(mode="json"))
    emit_trace(event)


def _record_raw_response_trace(
    *,
    request: RouterMessageRequest,
    raw_response: dict[str, Any],
    content: str,
    include_trace: bool,
    trace_events: list[dict[str, Any]],
) -> None:
    logger.info(
        "llm.plan.raw_response session_id=%s model=%s finish_reason=%s usage=%s content=%s",
        request.sessionId,
        raw_response.get("model"),
        _finish_reason(raw_response),
        raw_response.get("usage"),
        _truncate_for_log(content, 4000),
    )
    if not include_trace:
        return

    event = AssistantTraceEvent(
        stage="llm_raw_response",
        title="LLM原始分析结果",
        summary=f"model={raw_response.get('model')}，finish_reason={_finish_reason(raw_response)}",
        data={
            "model": raw_response.get("model"),
            "finish_reason": _finish_reason(raw_response),
            "usage": raw_response.get("usage"),
            "content": content,
        },
    )
    trace_events.append(event.model_dump(mode="json"))
    emit_trace(event)


def _parse_plan_payload(
    request: RouterMessageRequest,
    content: str,
) -> tuple[dict[str, Any], PlannerOutput]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PlannerError(f"LLM planner output is not JSON: {exc}") from exc

    logger.info(
        "llm.plan.parsed_json session_id=%s payload=%s",
        request.sessionId,
        _truncate_for_log(json.dumps(payload, ensure_ascii=False), 4000),
    )
    try:
        return payload, PlannerOutput.model_validate(payload)
    except ValidationError as exc:
        raise PlannerError(f"LLM planner output failed schema validation: {exc}") from exc


def _validate_plan_intents(
    request: RouterMessageRequest,
    plan: PlannerOutput,
    task_state: TaskRuntimeState,
    prompt: Any,
    harness: PromptHarness,
) -> None:
    allowed_intents = _allowed_intents_for_loaded_skills(prompt, harness)
    if not allowed_intents:
        return

    existing_task_intents = _existing_task_intents(task_state)
    emitted: list[tuple[str, str, str | None]] = []
    if plan.intent_code:
        emitted.append(("intent_code", plan.intent_code, None))
    if plan.recognition is not None and plan.recognition.intent_code:
        emitted.append(("recognition.intent_code", plan.recognition.intent_code, None))
    for index, task in enumerate(plan.task_list):
        emitted.append((f"task_list[{index}].intent_code", task.intent_code, task.taskId))
    if plan.current_task is not None:
        emitted.append(("current_task.intent_code", plan.current_task.intent_code, plan.current_task.taskId))

    invalid = [
        {"field": field, "intent_code": intent_code}
        for field, intent_code, task_id in emitted
        if intent_code not in allowed_intents
        and (task_id is None or existing_task_intents.get(task_id) != intent_code)
    ]
    if invalid:
        raise PlannerError(
            "LLM planner emitted intent_code not declared by loaded skills: "
            f"session_id={request.sessionId} invalid={invalid} allowed={sorted(allowed_intents)}"
        )


def _existing_task_intents(task_state: TaskRuntimeState) -> dict[str, str]:
    task_intents = {task.taskId: task.intent_code for task in task_state.task_list}
    if task_state.current_task is not None:
        task_intents.setdefault(task_state.current_task.taskId, task_state.current_task.intent_code)
    return task_intents


def _allowed_intents_for_loaded_skills(prompt: Any, harness: PromptHarness) -> set[str]:
    allowed: set[str] = set()
    for skill_name in getattr(prompt, "loaded_skills", ()):
        skill = harness.skills.get(str(skill_name))
        if skill is not None:
            allowed.update(skill.intent_codes)
    return allowed


def _skill_context_maps(prompt: Any, harness: PromptHarness) -> dict[str, dict[str, list[str]]]:
    skill_intent_map: dict[str, list[str]] = {}
    intent_skill_map: dict[str, list[str]] = {}
    reference_skill_map: dict[str, list[str]] = {}
    loaded_references = set(getattr(prompt, "loaded_references", ()))
    for skill_name in getattr(prompt, "loaded_skills", ()):
        skill = harness.skills.get(str(skill_name))
        if skill is None:
            continue
        skill_intent_map[skill.name] = list(skill.intent_codes)
        for intent_code in skill.intent_codes:
            intent_skill_map.setdefault(intent_code, []).append(skill.name)
        for reference in skill.references:
            if reference.id in loaded_references:
                reference_skill_map.setdefault(reference.id, []).append(skill.name)
            scoped_id = f"{skill.name}:{reference.id}"
            if scoped_id in loaded_references:
                reference_skill_map.setdefault(scoped_id, []).append(skill.name)
    return {
        "skill_intent_map": skill_intent_map,
        "intent_skill_map": intent_skill_map,
        "reference_skill_map": reference_skill_map,
    }


def _finish_reason(raw_response: dict) -> str | None:
    try:
        return raw_response["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _effective_intent_code(plan: PlannerOutput) -> str | None:
    if plan.intent_code is not None:
        return plan.intent_code
    if plan.recognition is not None:
        return plan.recognition.intent_code
    return None


def _has_active_task(task_state: TaskRuntimeState) -> bool:
    if task_state.current_task is not None and task_state.current_task.status not in {
        "completed",
        "cancelled",
        "failed",
    }:
        return True
    return any(task.status not in {"completed", "cancelled", "failed"} for task in task_state.task_list)


def _first_active_task(task_list: list[Any]) -> Any | None:
    for task in task_list:
        if getattr(task, "status", None) not in {"completed", "cancelled", "failed"}:
            return task
    return None


def _task_json(task: Any) -> dict[str, Any]:
    if hasattr(task, "model_dump"):
        return task.model_dump(mode="json")
    if isinstance(task, dict):
        return dict(task)
    return {}


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n...[truncated]"


def _required_slots(skill: SkillDocument) -> tuple[str, ...]:
    return skill.required_slots


def _default_slot_reference_ids(skill: SkillDocument) -> tuple[str, ...]:
    return tuple(
        reference.id
        for reference in skill.references
        if reference.id in {"slot_filling", "slot_rules"}
        and not is_workflow_tool_reference_body(reference.body)
    )


def _slot_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _missing_slots_message(skill: SkillDocument, missing_slots: list[str]) -> str:
    if missing_slots:
        return "请补充当前任务所需信息：" + "、".join(missing_slots)
    return "请补充当前任务所需信息"


def _task_for_log(task: object | None) -> str:
    if task is None:
        return "null"
    if hasattr(task, "model_dump"):
        return _truncate_for_log(json.dumps(task.model_dump(mode="json"), ensure_ascii=False), 1200)
    return _truncate_for_log(str(task), 1200)


def _llm_context_json(value: Any) -> str:
    return json.dumps(_sanitize_llm_context(value), ensure_ascii=False)


def _sanitize_llm_context(value: Any) -> Any:
    """Remove server-owned identity/session values before rendering LLM prompts."""
    if isinstance(value, list):
        cleaned_items = []
        for item in value:
            cleaned = _sanitize_llm_context(item)
            if cleaned is not _OMIT:
                cleaned_items.append(cleaned)
        return cleaned_items
    if isinstance(value, tuple):
        return _sanitize_llm_context(list(value))
    if isinstance(value, dict):
        if _is_private_config_variable(value):
            return _OMIT
        cleaned: dict[str, Any] = {}
        for key, item_value in value.items():
            if _is_private_context_key(str(key)):
                continue
            cleaned_value = _sanitize_llm_context(item_value)
            if cleaned_value is not _OMIT:
                cleaned[key] = cleaned_value
        return cleaned
    return value


class _OmitValue:
    pass


_OMIT = _OmitValue()


def _is_private_config_variable(value: dict[str, Any]) -> bool:
    name = value.get("name")
    return isinstance(name, str) and _is_private_context_key(name)


def _is_private_context_key(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    compact = normalized.replace("_", "")
    return normalized in _LLM_PRIVATE_CONTEXT_KEYS or compact in _LLM_PRIVATE_CONTEXT_KEYS


def _string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _merge_strings(values: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _truncate_for_log(value: str, limit: int) -> str:
    value = value.replace("\r", "\\r").replace("\n", "\\n")
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}...[truncated]"

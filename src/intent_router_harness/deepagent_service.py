"""Backward-compatible re-exports from the ``deepagent`` subpackage.

All symbols that were previously defined in this module are now organized
in ``intent_router_harness.deepagent.*``.  Existing import paths continue
to work; new code should import from the subpackage directly.
"""

from intent_router_harness.contracts import AssistantTraceEvent  # noqa: F401
from intent_router_harness.deepagent.errors import (  # noqa: F401
    DeepAgentRuntimeError,
    SessionRunInProgressError,
    WorkflowApiCallError,
    WorkflowHttpError,
    WorkflowNoNodeOutputError,
    WorkflowSseParseError,
    WorkflowTimeoutError,
    WorkflowUrlNotAllowedError,
)
from intent_router_harness.deepagent.helpers import (  # noqa: F401
    _deepagent_user_payload,
    _harness_context_block,
    _harness_virtual_files,
    _loads_json_object,
    _result_with_workflow_call,
    _result_with_workflow_final_output,
    _workflow_request_from_content,
    _workflow_session_id,
)
from intent_router_harness.deepagent.middleware import (  # noqa: F401
    _deepagent_runtime_middleware,
)
from intent_router_harness.deepagent.runner import (  # noqa: F401
    NativeDeepAgentRunner,
    WorkflowApiCallInput,
    WorkflowCallSnapshot,
    _deepagent_graph_recursion_limit,
    _deepagent_system_prompt,
    _parse_deepagent_response,
    _resolve_deepagent_model,
    _try_parse_deepagent_response,
)
from intent_router_harness.deepagent.service import (  # noqa: F401
    DeepAgentAssistantProtocolService,
    DeepAgentRunContext,
    DeepAgentRunner,
    DeepAgentRunResult,
    SessionRunLockStore,
)

from typing import TypeVar, Generator, Union, List, Any, Dict, Optional, AsyncGenerator
import logging
from mcp.types import CallToolResult, TextContent, Tool, EmbeddedResource
from pydantic import BaseModel
import fastmcp
import re
import datetime
import json
import time
from app.oai_models import ChatCompletionStreamResponse, ChatCompletionMessageParam, ErrorResponse, random_uuid
import regex
import asyncio
from functools import wraps
from starlette.concurrency import run_in_threadpool
from functools import partial
from typing import Callable

def get_file_extension(uri: str) -> str:
    logger.info(f"received uri: {uri[:100]}")
    if uri.startswith('data:'):
        return uri.split(';')[0].split('/')[-1].lower()
    
    if uri.startswith('http'):
        return uri.split('.')[-1].lower()
    
    if uri.startswith('file:'):
        return uri.split('.')[-1].lower()
    
    return None

class Attachment:
    def __init__(self, data_uri: str, name: Optional[str] = None, type: Optional[str] = None):
        self.data_uri = data_uri
        self.type = type or get_file_extension(data_uri) or "data"
        self.name = name or f"attachment_{random_uuid()}.{self.type}"

class AgentResourceManager:
    def __init__(self):
        self.resources: dict[str, str] = {}
        self.attachments: list[Attachment] = []
        self.data_uri_pattern = regex.compile(r'data:[^;,]+;base64,[A-Za-z0-9+/]+=*', regex.IGNORECASE | regex.DOTALL)
        self.resource_citing_pattern = regex.compile(r'"([^"]+)"', regex.IGNORECASE | regex.DOTALL)

    def embed_resource(self, content: str) -> str:
        def replace_with_id(match: regex.Match[str]) -> str:
            resource_id = random_uuid()
            self.resources[resource_id] = match.group(0)
            return resource_id

        return self.data_uri_pattern.sub(replace_with_id, content)

    def extract_resource(self, content: str) -> list[str]:
        results: list[str] = []

        for m in self.resource_citing_pattern.finditer(content):
            resource_id: str = m.group(1)

            if resource_id not in results and resource_id in self.resources:
                results.append(resource_id)

        for resource_id, _ in self.resources.items():
            if resource_id in content and resource_id not in results:
                results.append(resource_id)

        return results

    def get_resource_by_id(self, resource_id: str) -> Optional[str]:
        return self.resources.get(resource_id)

    def reveal_resource(self, content: str) -> str:
        def replace_with_data_uri(match: regex.Match[str]) -> str:
            resource_id = match.group(1)
            return f'"{self.resources.get(resource_id, resource_id)}"'

        return self.resource_citing_pattern.sub(replace_with_data_uri, content)
    
    def add_attachment(self, data_uri: str, name: Optional[str] = None) -> Attachment:
        attachment = Attachment(data_uri, name)
        self.attachments.append(attachment)
        return attachment

    async def handle_streaming_response(self, stream: AsyncGenerator[ChatCompletionStreamResponse | ErrorResponse, None]) -> AsyncGenerator[ChatCompletionStreamResponse | ErrorResponse, None]:
        buffer: str = ''
        citing_pat = regex.compile(r"<(file|img|data)\b[^>]*>(.*?)</\1>|<(file|img|data)\b[^>]*/>", regex.DOTALL | regex.IGNORECASE)

        async for chunk in stream:
            if isinstance(chunk, ErrorResponse):
                yield chunk
                continue

            buffer += chunk.choices[0].delta.content or ''
            partial_match = citing_pat.search(buffer, partial=True)

            if not partial_match or (partial_match.span()[0] == partial_match.span()[1]):
                yield wrap_chunk(random_uuid(), buffer, 'assistant')
                buffer = ''

                continue

            if partial_match.partial:
                yield wrap_chunk(random_uuid(), buffer[:partial_match.span()[0]], 'assistant')
                buffer = buffer[partial_match.span()[0]:]

                continue

            yield wrap_chunk(random_uuid(), self.reveal_resource(buffer), 'assistant')
            buffer = ''

        if buffer:
            yield wrap_chunk(random_uuid(), buffer, 'assistant')


logger = logging.getLogger(__name__)
T = TypeVar('T')

def batching(generator: Union[Generator[T, None, None], List[T]], batch_size: int) -> Generator[list[T], None, None]:

    if isinstance(generator, List):
        for i in range(0, len(generator), batch_size):
            yield generator[i:i+batch_size]

    elif isinstance(generator, Generator) or hasattr(generator, "__iter__"):
        batch = []

        for item in generator:
            batch.append(item)

            if len(batch) == batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    else:
        raise ValueError("Generator must be a generator or a list")
    

def convert_mcp_tools_to_openai_format(
    mcp_tools: List[Any]
) -> List[Dict[str, Any]]:
    """Convert MCP tool format to OpenAI tool format"""
    openai_tools = []
    
    logger.debug(f"Input mcp_tools type: {type(mcp_tools)}")
    logger.debug(f"Input mcp_tools: {mcp_tools}")
    
    # Extract tools from the response
    if hasattr(mcp_tools, 'tools'):
        tools_list = mcp_tools.tools
        logger.debug("Found ListToolsResult, extracting tools attribute")
    elif isinstance(mcp_tools, dict):
        tools_list = mcp_tools.get('tools', [])
        logger.debug("Found dict, extracting 'tools' key")
    else:
        tools_list = mcp_tools
        logger.debug("Using mcp_tools directly as list")
        
    logger.debug(f"Tools list type: {type(tools_list)}")
    logger.debug(f"Tools list: {tools_list}")
    
    # Process each tool in the list
    if isinstance(tools_list, list):
        logger.debug(f"Processing {len(tools_list)} tools")
        for tool in tools_list:
            logger.debug(f"Processing tool: {tool}, type: {type(tool)}")
            if hasattr(tool, 'name') and hasattr(tool, 'description'):
                openai_name = sanitize_tool_name(tool.name)
                logger.debug(f"Tool has required attributes. Name: {tool.name}")
                
                tool_schema = getattr(tool, 'inputSchema', {})
                (tool_schema.setdefault(k, v) for k, v in {
                    "type": "object",
                    "properties": {},
                    "required": []
                }.items()) 
                                
                openai_tool = {
                    "type": "function",
                    "function": {
                        "name": openai_name,
                        "description": tool.description,
                        "parameters": tool_schema
                    }
                }

                openai_tools.append(openai_tool)
                logger.debug(f"Converted tool {tool.name} to OpenAI format")
            else:
                logger.debug(
                    f"Tool missing required attributes: "
                    f"has name = {hasattr(tool, 'name')}, "
                    f"has description = {hasattr(tool, 'description')}"
                )
    else:
        logger.debug(f"Tools list is not a list, it's a {type(tools_list)}")
    
    return openai_tools

def sanitize_tool_name(name: str) -> str:
    """Sanitize tool name for OpenAI compatibility"""
    # Replace any characters that might cause issues
    return name.replace("-", "_").replace(" ", "_").lower()

def compare_toolname(openai_toolname: str, mcp_toolname: str) -> bool:
    return sanitize_tool_name(mcp_toolname) == openai_toolname
    
async def execute_openai_compatible_toolcall(
    toolname: str, arguments: Dict[str, Any], mcp: fastmcp.FastMCP
) -> list[Union[TextContent, EmbeddedResource]]:
    tools = await mcp._mcp_list_tools()
    candidate: List[Tool] = []

    for tool in tools:
        tool: Tool
        if compare_toolname(toolname, tool.name):
            candidate.append(tool)

    if len(candidate) > 1:
        logger.warning(
            "More than one tool has the same santizied"
            " name to the requested tool"
        )
        
    elif len(candidate) == 0:
        return CallToolResult(
            content=[TextContent(text=f"Tool {toolname} not found", type="text")], 
            isError=True
        )
        
    toolname = candidate[0].name

    try:
        res = await mcp._mcp_call_tool(toolname, arguments)

        if isinstance(res, tuple) and len(res) == 2:
            res = res[1].get("result", "empty result")
  
    except Exception as e:
        logger.error(f"Error executing tool {toolname} with arguments {arguments}: {e}")
        return CallToolResult(
            content=[TextContent(text=f"Error executing tool {toolname}: {e}", type="text")], 
            isError=True
        )

    return res
    
def strip_marker(content: str, marker: str, outter_only: bool = False, replace: str = "") -> str:
    # Remove self-closing tags like <marker ... />
    self_closing_pat = re.compile(f"<{marker}\\b[^>]*/>", re.IGNORECASE)
    content = self_closing_pat.sub(replace, content)

    if not outter_only:
        # Remove full element including its content
        pat = re.compile(f"<{marker}\\b[^>]*>.*?</{marker}>", re.DOTALL | re.IGNORECASE)
        content = pat.sub(replace, content)
    else:
        # Remove tag only, keep inner text
        pat = re.compile(f"<{marker}\\b[^>]*>(.*?)</{marker}>", re.DOTALL | re.IGNORECASE)
        content = pat.sub(lambda m: m.group(1).strip() or replace, content)

    return content

def strip_markers(content: str, markers: tuple[str, bool, str]) -> str:
    for tup in markers:
        if not len(tup):
            continue

        if len(tup) == 1:
            marker, outter_only, replace = tup[0], False, ""

        if len(tup) == 2:
            marker, outter_only, replace = tup[0], tup[1], ""

        if len(tup) == 3:
            marker, outter_only, replace = tup

        content = strip_marker(content, marker, outter_only, replace)

    return content

def refine_mcp_response(something: Any, arm: AgentResourceManager, skip_embed_resource: bool = False) -> str:
    if isinstance(something, dict):
        return {
            k: refine_mcp_response(v, arm, skip_embed_resource)
            for k, v in something.items()
        }

    elif isinstance(something, (list, tuple)):
        return [
            refine_mcp_response(v, arm, skip_embed_resource)
            for v in something
        ]

    elif isinstance(something, BaseModel):
        return refine_mcp_response(something.model_dump(), arm, skip_embed_resource)

    elif isinstance(something, str):
        if not skip_embed_resource:
            something = arm.embed_resource(something)

        return strip_markers(
            something, 
            (
                ("agent_message", True), 
                ("think", False), 
                ("details", False), 
                ("action", False)
            )
        ).strip()

    return something

def refine_chat_history(messages: list[dict[str, str]], system_prompt: str, arm: AgentResourceManager) -> list[dict[str, str]]:
    refined_messages = []

    current_time_utc_str = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    system_prompt += f'\nNote: Current time is {current_time_utc_str} UTC (only use this information when being asked or for searching purposes)'

    has_system_prompt = False
    attachments: list[tuple[Attachment, int]] = []

    for i, message in enumerate(messages):
        message: dict[str, str]

        if isinstance(message, dict) and message.get('role', 'undefined') == 'system':
            message['content'] += f'\n{system_prompt}'
            has_system_prompt = True
            refined_messages.append(message)
            continue
    
        if isinstance(message, dict) \
            and message.get('role', 'undefined') == 'user' \
            and isinstance(message.get('content'), list):

            content = message['content']
            text_input = ''

            for item in content:
                item: dict[str, Any]

                if item.get('type', 'undefined') == 'text':
                    text_input += item.get('text') or ''

                elif item.get('type', 'undefined') == 'file':
                    file_item: dict[str, Any] = item.get('file', {})
                    file_data = file_item.get('file_data')
                    file_name = file_item.get('file_name')

                    if file_data:
                        attachments.append((arm.add_attachment(file_data, file_name), i))

                elif item.get('type', 'undefined') == 'image_url':
                    image_url: dict[str, Any] = item.get('image_url', {})
                    url = image_url.get('url', '')
                    attachments.append((arm.add_attachment(url or ''), i))

            refined_messages.append({
                "role": "user",
                "content": text_input
            })

        else:
            raw_content = message.get("content", "")
            embeded_content = arm.embed_resource(raw_content)

            _message = {
                "role": message.get('role', 'assistant'),
                "content": strip_markers(
                    embeded_content, 
                    (
                        ("agent_message", False), 
                        ("think", False), 
                        ("details", False), 
                        ("action", False)
                    )
                )
            }

            refined_messages.append(_message)

    if not has_system_prompt and system_prompt != "":
        refined_messages.insert(0, {
            "role": "system",
            "content": system_prompt
        })

    if isinstance(refined_messages[-1], str):
        refined_messages[-1] = {
            "role": "user",
            "content": refined_messages[-1]
        }

    return refined_messages

def refine_assistant_message(
    assistant_message: Union[dict[str, str], BaseModel]
) -> dict[str, str]:
    
    if isinstance(assistant_message, BaseModel):
        assistant_message = assistant_message.model_dump()

    if 'content' in assistant_message:
        assistant_message['content'] = strip_markers(
            assistant_message['content'] or "", 
            (
                ("agent_message", False), 
                ("think", False), 
                ("details", False), 
                ("img", False), 
                ("action", False)
            )
        )

    return assistant_message


def get_newest_message(messages: list[ChatCompletionMessageParam]) -> str:
    if isinstance(messages[-1].get("content", ""), str):
        return messages[-1].get("content", "")
    
    elif isinstance(messages[-1].get("content", []), list):
        for item in messages[-1].get("content", []):
            if item.get("type") == "text":
                return item.get("text", "")

    else:
        raise ValueError(f"Invalid message content: {messages[-1].get('content')}")
    

async def wrap_toolcall_request(uuid: str, fn_name: str, args: dict[str, Any]) -> ChatCompletionStreamResponse:
    args_str = json.dumps(args, indent=2)
    
    template = f'''
<action>Executing <b>{fn_name}</b></action>

<details>
<summary>
Arguments:
</summary>

```json
{args_str}
```

</details>
'''

    return ChatCompletionStreamResponse(
        id=uuid,
        object='chat.completion.chunk',
        created=int(time.time()),
        model='unspecified',
        choices=[
            dict(
                index=0,
                delta=dict(
                    content=template,
                    role='tool'
                ),
            )
        ]
    )


def levenshtein_distance(a: str, b: str, norm: bool = True) -> int:
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]

    for i in range(n + 1):
        dp[i][0] = i  # Deleting all from a

    for j in range(m + 1):
        dp[0][j] = j  # Inserting all to a

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if a[i - 1] == b[j - 1]:
                cost = 0
            else:
                cost = 1

            dp[i][j] = min(
                dp[i - 1][j] + 1,      # Deletion
                dp[i][j - 1] + 1,      # Insertion
                dp[i - 1][j - 1] + cost  # Substitution
            )

    return dp[n][m] / max(n, m, 1) if norm else dp[n][m]

def wrap_chunk(id: str, content: str, role: str) -> ChatCompletionStreamResponse:
    return ChatCompletionStreamResponse(
        id=id,
        object='chat.completion.chunk',
        created=int(time.time()),
        model='unspecified',
        choices=[
            dict(
                index=0,
                delta=dict(
                    content=content,
                    role=role
                ),
            )
        ]
    )


from app.utils import AgentResourceManager, strip_markers, get_file_extension

def create_attachment(data_uri: str, base_name: str) -> dict:
    ext = get_file_extension(data_uri) or 'data'
    
    # is image
    if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'heif']:
        return {
            "type": "image_url",
            "image_url": {
                "url": data_uri
            }
        }

    return {
        "type": "file",
        "file": {
            "filename": f"{base_name}.{ext}",
            "file_data": data_uri,
        }
    }
    

def create_rich_user_message(message: str, arm: AgentResourceManager) -> dict[str, Any]:
    attachments: list[str] = arm.extract_resource(message)
    
    if len(attachments) == 0:
        return {
            "role": "user",
            "content": message
        }

    message = strip_markers(
        message, 
        (
            ("img", False),
            ("file", False),
            ("data", False)
        )
    )

    content = [
        {
            "type": "text",
            "text": message
        }
    ]

    for attachment in attachments:
        attachment_data = arm.get_resource_by_id(attachment)

        if attachment_data:
            content.append(create_attachment(attachment_data, base_name=attachment))

    return {
        "role": "user",
        "content": content
    }

def sync2async(sync_func: Callable):
    async def async_func(*args, **kwargs):
        res = run_in_threadpool(partial(sync_func, *args, **kwargs))

        if isinstance(res, (Generator, AsyncGenerator)):
            return res

        return await res

    return async_func if not asyncio.iscoroutinefunction(sync_func) else sync_func

def limit_asyncio_concurrency(num_of_concurrent_calls: int):
    semaphore = asyncio.Semaphore(num_of_concurrent_calls)

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with semaphore:
                res = func(*args, **kwargs)

                if isinstance(res, (Generator, AsyncGenerator)):
                    return res

                return await res

        return wrapper
    return decorator

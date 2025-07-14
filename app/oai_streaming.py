from .oai_models import ChatCompletionResponse, ChatCompletionStreamResponse, ToolCall, random_uuid, ErrorResponse
import httpx
import json
from typing import AsyncGenerator
import logging
from json_repair import repair_json

def repair_json_no_except(json_str: str) -> str:
    try:
        res = repair_json(json_str)

        if isinstance(res, tuple) and len(res) > 0:
            return res[0]

        return res

    except Exception as e:
        logger.info(f"failed to repair json string {json_str}: {e}")
        return json_str

logger = logging.getLogger(__name__)

def reconstruct_curl_request(
    base_url: str,
    api_key: str,
    completion_path: str = "/chat/completions",
    **payload_to_call
) -> str:
    return f'curl -X POST "{base_url}/{completion_path}" -H "Authorization: Bearer {api_key}" -H "Content-Type: application/json" -d \'{json.dumps(payload_to_call)}\''

class ChatCompletionResponseBuilder:
    def __init__(self):
        self.msg, self.calls_by_idx, self.finished_reason, self.model_id, self.completion_id = '', {}, '', '', ''
        self.calls = []

    def add_chunk(self, chunk: ChatCompletionStreamResponse):
        choice = chunk.choices[0]

        if choice.delta.content:
            self.msg += choice.delta.content

        elif choice.delta.tool_calls:
            for tool_call in choice.delta.tool_calls:
                # call_idx = tool_call.index

                # if call_idx not in self.calls_by_idx:
                #     self.calls_by_idx[call_idx] = {
                #         "id": tool_call.id,
                #         "type": tool_call.type,
                #         "function": {
                #             "name": tool_call.function.name,
                #             "arguments": tool_call.function.arguments or ""
                #         }
                #     }

                # else:
                #     self.calls_by_idx[call_idx]["function"]["arguments"] += tool_call.function.arguments
                
                if tool_call.function.name is not None:
                    self.calls.append({
                        "id": "call_" + random_uuid()[-20:],
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments or ""
                        }
                    })

                elif len(self.calls) > 0:
                    self.calls[-1]["function"]["arguments"] += tool_call.function.arguments

        self.finished_reason = choice.finish_reason
        self.model_id = chunk.model
        self.completion_id = chunk.id
        return chunk

    async def build(self) -> ChatCompletionResponse:
        verified_calls = []

        for call in self.calls:
            try:
                call["function"]["arguments"] = repair_json_no_except(call["function"]["arguments"])
                json.loads(call["function"]["arguments"])
                ToolCall.model_validate(call)
                verified_calls.append(call)

            except Exception as e:
                logger.error(f"failed to verify call {call['id']}: {e}; Raw call: {call} (Skipping)")
                continue

        return ChatCompletionResponse.model_validate(
            dict(
                model=self.model_id,
                choices=[
                    dict(
                        index=0,
                        message=dict(
                            role="assistant",
                            content=self.msg,
                            tool_calls=verified_calls
                        ),
                        finish_reason=self.finished_reason
                    )
                ],
                usage=dict(
                    prompt_tokens=0,
                    completion_tokens=0
                )
            )
        )

async def create_streaming_response(
    base_url: str,
    api_key: str,
    completion_path: str = "/chat/completions",
    **payload_to_call
) -> AsyncGenerator[ChatCompletionStreamResponse, None]:
    completion_path = completion_path.strip("/")
    logger.info(f"Streaming response to {base_url}/{completion_path}")
    payload_to_call.pop("stream", None)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        async with client.stream(
            "POST",
            f"{base_url}/{completion_path}",
            json={
                **payload_to_call,
                'stream': True
            },
            headers={
                'Authorization': f'Bearer {api_key}'
            },
            timeout=httpx.Timeout(60.0 * 10)
        ) as response:

            try:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    while line.startswith('data: '):    
                        line = line[6:].strip()

                    if line == "": 
                        continue
                    
                    # check if the line is ping 
                    if line.startswith(": ping"):
                        continue

                    if line == "[DONE]": 
                        break

                    try:
                        resp_json = json.loads(line)

                        if "error" in resp_json:
                            errr_obj = resp_json.get("error", {})
                            
                            if isinstance(errr_obj, dict):
                                yield ErrorResponse.model_validate(errr_obj)

                            else:
                                yield ErrorResponse(message=errr_obj, type="unknown_error")

                    except Exception as e:
                        raise e

                    if resp_json.get('object', '') == 'chat.completion.chunk':
                        yield ChatCompletionStreamResponse.model_validate(resp_json)

            except Exception as e:
                logger.error(f"Failed to stream response: {e}")

                curl_command = reconstruct_curl_request(
                    base_url,
                    api_key,
                    completion_path,
                    **payload_to_call,
                    stream=True,
                )

                logger.error(f"Failed to stream response: {e}\n{curl_command}")
                raise e
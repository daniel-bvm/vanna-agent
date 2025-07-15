from vanna.openai import OpenAI_Chat
from functools import lru_cache
from vanna.chromadb import ChromaDB_VectorStore
from app.db_models import (
    DatabaseAuth, 
    PostgresAuth, 
    MSSQLAuth, 
    MySQLAuth, 
    SQLiteAuth, 
    DuckDBAuth, 
    SnowflakeAuth, 
    BigQueryAuth, 
    OracleAuth, 
    OtherDBAuth
)
import logging
from app.configs import settings
from openai import OpenAI
from typing import Any
import asyncio
from app.utils import sync2async, wrap_chunk, refine_chat_history, refine_mcp_response, refine_assistant_message, AgentResourceManager
from app.oai_models import ChatCompletionRequest, ChatCompletionAdditionalParameters, ChatCompletionStreamResponse, ChatCompletionResponse, ErrorResponse, random_uuid
from app.oai_streaming import ChatCompletionResponseBuilder, create_streaming_response
from typing import Optional, AsyncGenerator, Generator, Callable
import json
import os
from pandas import DataFrame
import base64
import plotly

logger = logging.getLogger(__name__) 

class VannaWrapper(ChromaDB_VectorStore, OpenAI_Chat):
    def __init__(self, config=None, client: OpenAI=None):
        ChromaDB_VectorStore.__init__(self, config=config)
        OpenAI_Chat.__init__(self, config=config, client=client)
        
async def wrapstream(
    streaming_iter: AsyncGenerator[ChatCompletionStreamResponse | ErrorResponse, None], 
    callback: Callable[[ChatCompletionStreamResponse | ErrorResponse], None]
):
    async for chunk in streaming_iter:
        callback(chunk)

        if chunk.choices[0].delta.content:
            yield chunk
            
IS_APP_SUPPORT_SVG = False

class Session:
    def __init__(self, vanna: VannaWrapper):
        self.vanna = vanna
        self.session_validated = False
        self.auth: DatabaseAuth = None
        
    def construct_system_prompt(self):
        SYSTEM_PROMPT = 'You are Vanna Agent, a database master who can manage and query to extract information, insight from the database.'

        schemas = self.get_schema()
        if self.healthcheck() and schemas is not None:
            SYSTEM_PROMPT += "\nStatus: Connected"
            SYSTEM_PROMPT += "\nDatabase: " + self.auth.db_type

            if isinstance(schemas, DataFrame):
                SYSTEM_PROMPT += "\nSchemas: "
                unique_tables = schemas["table_name"].unique()

                for table in unique_tables:
                    filtered_schema = schemas[schemas["table_name"] == table]
                    selected_columns = ["column_name", "data_type"]
                    
                    SYSTEM_PROMPT += "\n\nTable: " + table
                    SYSTEM_PROMPT += "\n" + filtered_schema[selected_columns].to_markdown()
                    
                if len(unique_tables) == 0:
                    SYSTEM_PROMPT += "\nNo schemas found"
                    
                else:
                    SYSTEM_PROMPT += """\nCRITICAL RULES:
1. ALWAYS use table names in your SQL queries
2. NEVER write column names without table prefixes when multiple tables are involved
3. Use proper table aliases (e.g., o for orders, p for products)
4. Always specify the table name before the column name: table_name.column_name
5. Use JOINs with explicit table names and conditions
6. When aggregating data, always specify which table the data comes from
7. always copy the exact source rendering when referencing a resource, either responding to user or messaging to other agents. For instance, use the precise format: <img src="{{src-id}}"/> (for images).
"""

        else:
            SYSTEM_PROMPT += "\nStatus: Disconnected (no actions available)"

        return SYSTEM_PROMPT
        
    async def validate(self, auth: DatabaseAuth, timeout: int = 10) -> bool:
        fn = None
        
        if isinstance(auth, PostgresAuth):
            fn = self.vanna.connect_to_postgres
        elif isinstance(auth, MSSQLAuth):
            fn = self.vanna.connect_to_mssql
        elif isinstance(auth, MySQLAuth):
            fn = self.vanna.connect_to_mysql
        elif isinstance(auth, SQLiteAuth):
            fn = self.vanna.connect_to_sqlite
        # suppport additional db type later

        if fn is None:
            return False

        if self.session_validated:
            logger.info("Invalidating current session") 
            self.invalidate()

        asyncfn = sync2async(fn)
        done, pending = await asyncio.wait(
            [asyncio.create_task(asyncfn(**auth.model_dump(exclude={"db_type"})))], 
            timeout=timeout
        )

        if len(pending) > 0:
            logger.error("Connection timeout")
            return False
        
        try:
            task_result = done.pop().result()
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False

        if isinstance(task_result, Exception):
            logger.error(f"Connection failed: {task_result}")
            return False
        
        self.session_validated = True
        self.auth = auth

        # return self._train()
        return True
    
    def healthcheck(self):
        # check if the connection is established or not
        if not self.session_validated:
            return False
        
        return True
        
    def invalidate(self):
        self.session_validated = False
        self.auth = None
        
    def get_schema(self) -> Optional[DataFrame]:
        if not self.session_validated:
            return None
        
        
        if isinstance(self.auth, PostgresAuth):
            query = f"""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_name IN (
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_type = 'BASE TABLE'
                )
            """

        elif isinstance(self.auth, MSSQLAuth):
            query = f"""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_name IN (
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_type = 'BASE TABLE'
                    AND table_schema = 'dbo'
                )
            """

        elif isinstance(self.auth, MySQLAuth):
            query = f"""
                SELECT TABLE_NAME as table_name, COLUMN_NAME as column_name, DATA_TYPE as data_type
                FROM information_schema.columns
                WHERE table_name IN (
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE 
                        table_type = 'BASE TABLE'
                )
            """

        # elif isinstance(self.auth, SQLiteAuth):
        #     query = f"""
        #         SELECT *
        #         FROM sqlite_master
        #         WHERE type = 'table'
        #         AND name IN ({user_created_tables})
        #     """

        else:
            logger.error("Unsupported database type")
            return None

        return self.vanna.run_sql(query)

    def _train(self):
        user_created_tables = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_type = 'BASE TABLE'
        """

        if isinstance(self.auth, PostgresAuth):
            info_schema = self.vanna.run_sql(f"""
                SELECT *
                FROM information_schema.columns
                WHERE table_name IN ({user_created_tables})
            """)
        
        elif isinstance(self.auth, MSSQLAuth):
            info_schema = self.vanna.run_sql(f"""
                SELECT *
                FROM information_schema.columns
                WHERE table_name IN ({user_created_tables})
            """)
        
        elif isinstance(self.auth, MySQLAuth):
            info_schema = self.vanna.run_sql(f"""
                SELECT *
                FROM information_schema.columns
                WHERE table_name IN ({user_created_tables})
            """)
        
        elif isinstance(self.auth, SQLiteAuth):
            info_schema = self.vanna.run_sql("SELECT type, sql FROM sqlite_master WHERE sql is not null")

        else:
            logger.error("Unsupported database type")
            return False

        plan = self.vanna.get_training_plan_generic(info_schema)
        self.vanna.train(plan=plan)

        return True
    
    async def analyse(self, reason: str, sql: str, visualize: bool = True) -> str:
        try:
            df: Optional[DataFrame] = await sync2async(self.vanna.run_sql)(sql)
        except Exception as e:
            return f"Error: {e}"

        if df is not None:
            result = df.head(30).to_markdown()

        else:
            return "Query returned an empty result"

        hidden_rows = len(df) - 30

        if hidden_rows > 0:
            result += f"\n... {hidden_rows} rows hidden"

        if visualize and len(df) > 0:
            try:
                plotly_code: str = await sync2async(self.vanna.generate_plotly_code)(
                    question=reason,
                    sql=sql,
                    df_metadata=f"Running df.dtypes gives:\n {df.dtypes}",
                )

                fig: plotly.graph_objs.Figure = await sync2async(self.vanna.get_plotly_figure)(
                    plotly_code=plotly_code, 
                    df=df
                )

                if IS_APP_SUPPORT_SVG:
                    img: bytes = await sync2async(fig.to_image)(format="svg")
                    b64: str = base64.b64encode(img).decode("utf-8")
                    b64 = f"data:image/svg+xml;base64,{b64}"
                else:
                    img: str = fig.to_image(format="png")
                    b64: str = base64.b64encode(img).decode("utf-8")
                    b64 = f"data:image/png;base64,{b64}"

                result += f'\n\nVisualization:\n<img src="{b64}"/>'

            except Exception as e:
                logger.error(f"Error generating visualization: {e}", exc_info=True)
                result += "\n\nNo visualization generated due to a system error"

        return result

    async def handle_request(
        self,
        request: ChatCompletionRequest, 
        event: asyncio.Event,
        additional_parameters: Optional[ChatCompletionAdditionalParameters] = None,
    ) -> AsyncGenerator[ChatCompletionStreamResponse | ChatCompletionResponse, None]:
        messages = request.messages
        assert len(messages) > 0, "No messages in the request"

        arm = AgentResourceManager()

        system_prompt = self.construct_system_prompt()
        messages: list[dict[str, Any]] = refine_chat_history(messages, system_prompt, arm)

        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "analyse",
                    "description": "Analyze the data in the database, return preview data (limit to 30 rows maximum)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "Why to run the query?"
                            },
                            "sql": {
                                "type": "string",
                                "description": "SQL query to run"
                            },
                            "visualize": {
                                "type": "boolean",
                                "description": "Whether to visualize the result",
                                "default": True
                            }
                        },
                        "required": ["sql"]
                    }
                }
            }
        ] if self.healthcheck() else []

        async def execute_toolcall(tool_name: str, tool_args: dict[str, Any]):
            if tool_name == "analyse":
                return await self.analyse(
                    tool_args["reason"], 
                    tool_args["sql"], 
                    tool_args.get("visualize", True)
                )
            
            return f"Tool {tool_name} not found!"

        finished = False
        n_calls, max_calls = 0, 25

        while not finished and not event.is_set():
            completion_builder = ChatCompletionResponseBuilder()
            requires_toolcall = n_calls < max_calls
            toolcalls = oai_tools

            payload = dict(
                messages=messages,
                tools=toolcalls,
                tool_choice="auto",
                model=settings.llm_model_id,
                # **(
                #     additional_parameters.model_dump() 
                #     if additional_parameters and 'api.openai.com' not in settings.llm_base_url 
                #     else {}
                # )
            )

            if not requires_toolcall:
                payload.pop("tools")
                payload.pop("tool_choice")

            streaming_iter = create_streaming_response(
                settings.llm_base_url,
                settings.llm_api_key,
                **payload
            )

            # need to reveal resource
            async for chunk in arm.handle_streaming_response(wrapstream(streaming_iter, completion_builder.add_chunk)):
                if event.is_set():
                    logger.info(f"[main] Event signal received, stopping the request")
                    break

                yield chunk

            completion = await completion_builder.build()
            messages.append(refine_assistant_message(completion.choices[0].message))

            for call in (completion.choices[0].message.tool_calls or []):
                if event.is_set():
                    logger.info(f"[toolcall] Event signal received, stopping the request")
                    break

                n_calls += 1

                _id, _name, _args = call.id, call.function.name, call.function.arguments
                _args: dict = json.loads(_args)
            
                yield wrap_chunk(random_uuid(), f"<action>Executing **{_name}**!</action>\n<details>\n<summary>Arguments:</summary>\n```json\n{json.dumps(_args, indent=2)}\n```\n</details>", "assistant")
                _result = await execute_toolcall(_name, _args)
                yield wrap_chunk(random_uuid(), f"<details>\n<summary>Result:</summary>\n\n{_result}\n\n</details>", "assistant")

                _result = refine_mcp_response(_result, arm)

                if not isinstance(_result, str):
                    try:
                        _result = json.dumps(_result)
                    except:
                        _result = str(_result)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": _id,
                        "content": _result
                    }
                )

            finished = len((completion.choices[0].message.tool_calls or [])) == 0

        os.makedirs("logs", exist_ok=True)
        with open(f"logs/messages-{request.request_id}.json", "w") as f:
            json.dump(messages, f, indent=2)

        yield completion

@lru_cache(maxsize=1)
def get_singleton_session() -> Session:
    session = Session(
        VannaWrapper(
            config=dict(
                model=settings.llm_model_id,
                temperature=0.4,
            ),
            client=OpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
            )
        )
    )

    return session
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
import pandas as pd
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
import regex

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
            
def summarize_dataframe(df: DataFrame) -> str:
    """Deep summary and quality check for DataFrame."""

    lines = []
    lines.append(f"🔹 Dataset: {len(df)} rows x {len(df.columns)} columns")

    # 1. Column overview
    lines.append("\n🔸 Column Overview:")
    for col in df.columns:
        dtype = df[col].dtype
        null_count = df[col].isnull().sum()
        unique_count = df[col].nunique()
        lines.append(f"  - {col} ({dtype}) | nulls: {null_count}, unique: {unique_count}")

    # 2. Numeric columns
    numeric_cols = df.select_dtypes(include=["number"]).columns
    if len(numeric_cols) > 0:
        lines.append("\n📊 Numeric Summary:")
        desc = df[numeric_cols].describe().T
        desc["skew"] = df[numeric_cols].skew()
        for col in desc.index:
            row = desc.loc[col]
            lines.append(
                f"  - {col}: mean={row['mean']:.2f}, std={row['std']:.2f}, min={row['min']}, max={row['max']}, skew={row['skew']:.2f}"
            )

    lines.append("\n⚠️ Potential Outliers (IQR Method):")
    for col in numeric_cols:
        q1 = df[col].quantile(0.25)
        q3 = df[col].quantile(0.75)
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr

        outliers = df[(df[col] < lower) | (df[col] > upper)]

        if not outliers.empty:
            top_outlier_vals = outliers[col].nlargest(3).round(2).tolist()

            lines.append(
                f"  - {col}: {len(outliers)} outliers | IQR=({q1:.2f}, {q3:.2f}) | Top values: {top_outlier_vals}"
            )
        else:
            lines.append(
                f"  - {col}: No outliers detected | IQR=({q1:.2f}, {q3:.2f})"
            )


    # 4. Object columns (categorical/text)
    object_cols = df.select_dtypes(include=["object"]).columns
    for col in object_cols:
        vc = df[col].value_counts().head(3)
        lines.append(f"\n🔠 Top values in '{col}':")
        for val, count in vc.items():
            lines.append(f"  - {val}: {count} rows")

    # 5. Datetime columns if any
    datetime_cols = df.select_dtypes(include=["datetime", "datetime64[ns]"]).columns
    for col in datetime_cols:
        lines.append(f"\n🗓️ Datetime column: {col}")
        lines.append(f"  - Range: {df[col].min()} → {df[col].max()}")

    return "\n".join(lines)

STOP_ANALYZING_MESSAGE = "I've tried my best to find useful information, but I couldn't find any."

REPORT_GEN_PROMPT = """
User Question:
{question}

Generated SQL:
{sql}

📊 Data Summary:
{data_summary}

🎯 Report Objective:
Write an analytical report that helps a data consumer understand the insight behind this query result.

👉 Important Guidelines:
- Do NOT assume significance unless justified by the data
- If the data is too uniform (e.g., all tax = 1), say so
- Highlight any skewed distributions or potential outliers
- Point out if a column has many nulls or low uniqueness
- Use clear bullet points or short summary paragraphs
- If there's no significant insight, say so briefly

✍️ Please generate a concise and data-grounded report:
"""

COT_TEMPLATE = """
You are an analytical assistant. The user asked:
"{user_request}"

Database schema:
{schema}

So far, these are the steps completed:
{context}

What is the next subquestion you should answer to help generate the report?
Respond in JSON format: {{ "subquestion": "...", "sql": "..." }}
If no more are needed, return: DONE, NO QUESTIONS ARE NEEDED!
"""

REPORT_EXTEND_PROMPT = """
🎯 Report Objective:
Write an analytical report that helps a data consumer understand the insight behind this query result.

👉 Important Guidelines:
- Do NOT assume significance unless justified by the data
- If the data is too uniform (e.g., all tax = 1), say so
- Highlight any skewed distributions or potential outliers
- Point out if a column has many nulls or low uniqueness
- Use clear bullet points or short summary paragraphs
- If there's no significant insight, say so briefly

✍️ Please generate a concise and data-grounded report:
"""

IS_APP_SUPPORT_SVG = False

class Session:
    def __init__(self, vanna: VannaWrapper):
        self.vanna = vanna
        self.session_validated = False
        self.auth: DatabaseAuth = None
        
    def fmt_schema(self) -> Optional[str]:
        schemas = self.get_schema()

        if schemas is None:
            return None

        unique_tables = schemas["table_name"].unique()

        if len(unique_tables) == 0:
            return ""

        result = ""

        for table in unique_tables:
            filtered_schema = schemas[schemas["table_name"] == table]
            selected_columns = ["column_name", "data_type"]
            
            result += "\n\nTable: " + table
            result += "\n" + filtered_schema[selected_columns].to_markdown()

        return result.strip()
        
    def construct_system_prompt(self):
        SYSTEM_PROMPT = 'You are Vanna Agent, a database master who can manage and query to extract information, insight from the database.'

        schemas = self.fmt_schema()
        if schemas is not None:
            SYSTEM_PROMPT += "\nStatus: Connected"
            SYSTEM_PROMPT += "\nDatabase: " + self.auth.db_type

            if isinstance(schemas, str):
                SYSTEM_PROMPT += "\nSchemas: "

                if len(schemas) == 0:
                    SYSTEM_PROMPT += "\nNo schemas found"

                else:
                    SYSTEM_PROMPT += "\n" + schemas
                    SYSTEM_PROMPT += f"""\n\nCRITICAL RULES:
0. The most important: write correct SQL for {self.auth.db_type} database.
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
    
    async def cot_analyse(self, question: str) -> AsyncGenerator[ChatCompletionStreamResponse | ChatCompletionResponse, None]:
        db_schema = self.fmt_schema()

        if not db_schema:
            raise ValueError("No database schema found")

        SCRATCHPAD = []

        max_steps = 25
        step = 0

        while True and step < max_steps:
            response: str = await sync2async(self.vanna.submit_prompt)(
                COT_TEMPLATE.format(
                    user_request=question, 
                    schema=db_schema,
                    context=[
                        {
                            "subquestion": e['subquestion'], 
                            "sql": e['sql'], 
                            "summary": e.get("summary", None)
                        } for e in SCRATCHPAD
                    ] if len(SCRATCHPAD) > 0 else "None yet."
                )
            )

            if "DONE, NO QUESTIONS ARE NEEDED!" in response.strip().upper():
                reasoning = None

                if 'reason' in response.lower():
                    try:
                        resp_json: dict[str, Any] = json.loads(response[response.find('{'):response.rfind('}')+1])
                        reasoning = resp_json.get('reason')
                    except Exception as err:
                        logger.error(f"Error parsing JSON: {err}; Response: {response}")
                        pass

                if not reasoning:
                    done_idx = response.upper().find('DONE')
                    after_done = response[done_idx+4:].strip()

                    if after_done:
                        reasoning = after_done

                break

            try:
                step_data: dict = json.loads(response[response.find('{'):response.rfind('}')+1])
            except Exception as e:
                logger.error(f"Failed to parse response at step {step}: {e}")
                step_data = {}

            subquestion = step_data.get('subquestion') or ""
            sql = step_data.get('sql') or ""

            if subquestion and sql:
                try:
                    df: Optional[DataFrame] = await sync2async(self.vanna.run_sql)(sql)
                    summary = df.describe().to_markdown() if df is not None else "No result"
                    head30 = df.head(30).to_markdown() if df is not None else "No result"

                    yield wrap_chunk(
                        random_uuid(),
                        f"<details>\n<summary>Details</summary>\n```sql\n{sql}\n```\n\n{summary}\n\n{head30}\n\n</details>", "assistant"
                    )

                    SCRATCHPAD.append({"subquestion": subquestion, "sql": sql, "summary": summary, "df": df})

                except Exception as e:
                    SCRATCHPAD.append({"subquestion": subquestion, "sql": sql, "summary": f"Error running SQL: {str(e)}", "df": None})

            else:
                SCRATCHPAD.append({"subquestion": subquestion or "No subquestion", "sql": sql or "No SQL", "summary": "Invalid action: Neither subquestion nor SQL provided", "df": None})

            step += 1

        VALID_SCRATCHPAD = [
            e for e in SCRATCHPAD if isinstance(e.get("df"), DataFrame)
        ]

        concatenated_df = (
            pd.concat([c['df'] for c in VALID_SCRATCHPAD], ignore_index=True) 
            if VALID_SCRATCHPAD else pd.DataFrame()
        )

        if len(concatenated_df) > 0:
            toolcall_result = 'Generated:'

            for i, e in enumerate(VALID_SCRATCHPAD):
                toolcall_result += "\nQuery {index}: {subquestion}\n```sql\n{sql}\n```\n".format(
                    index=i+1, 
                    subquestion=e['subquestion'], 
                    sql=e['sql']
                )

            toolcall_result += "\n\nInsights:\n" + summarize_dataframe(concatenated_df)

        else:
            toolcall_result = STOP_ANALYZING_MESSAGE
            
            if len(VALID_SCRATCHPAD) > 0:
                toolcall_result += "\n\nHere are the queries I tried:\n"

                for i, e in enumerate(VALID_SCRATCHPAD):
                    toolcall_result += "\nQuery {index}: {subquestion}\n```sql\n{sql}\n```\n".format(
                        index=i+1, 
                        subquestion=e['subquestion'], 
                        sql=e['sql']
                    )

        yield wrap_chunk(random_uuid(), toolcall_result, "assistant")

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
                result += "\nHere is the insight of the query result:\n"
                result += df.describe().to_markdown()

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
                    "name": "cot_analyse",
                    "description": "Start the analysis process",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "What to analyze?"
                            }
                        },
                        "required": ["question"]
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

        async def handoff(tool_name: str, tool_args: dict[str, Any]) -> AsyncGenerator[ChatCompletionStreamResponse | ChatCompletionResponse, None]:
            if tool_name == "cot_analyse":
                async for chunk in self.cot_analyse(tool_args["question"]):
                    yield chunk

            else:
                yield wrap_chunk(random_uuid(), f"Tool {tool_name} not found!", "assistant")

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
            has_success_toolcall = False
            
            toolcalls_requested = (completion.choices[0].message.tool_calls or [])

            for call_idx, call in enumerate(toolcalls_requested):
                if event.is_set():
                    logger.info(f"[toolcall] Event signal received, stopping the request")
                    break

                n_calls += 1

                _id, _name, _args = call.id, call.function.name, call.function.arguments
                _args: dict = json.loads(_args)
                _result = ""

                yield wrap_chunk(random_uuid(), f"<action>Analyzing...</action>", "assistant")

                async for chunk in handoff(_name, _args):
                    if regex.search(r"<details>.*<\/details>", chunk.choices[0].message.content or "") is None:
                        yield chunk

                    if isinstance(chunk, ErrorResponse):
                        raise Exception(chunk.message)
                    
                    _result += chunk.choices[0].message.content or ""

                _result = refine_mcp_response(_result, arm)

                if not isinstance(_result, str):
                    try:
                        _result = json.dumps(_result)
                    except:
                        _result = str(_result)

                has_success_toolcall = has_success_toolcall or STOP_ANALYZING_MESSAGE not in _result

                if has_success_toolcall and call_idx == len(toolcalls_requested) - 1:
                    _result += "\n\n" + REPORT_EXTEND_PROMPT

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": _id,
                        "content": _result
                    }
                )

            finished = len(toolcalls_requested) == 0

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
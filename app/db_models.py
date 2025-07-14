from pydantic import BaseModel, Field
from enum import Enum
from typing import TypeVar, Union, Optional, Literal
from pathlib import Path

class DBType(str, Enum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    MSSQL = "mssql"
    DUCKDB = "duckdb"
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"
    ORACLE = "oracle"
    OTHER = "other"

db_type = TypeVar("db_type", bound=DBType)

# Base authorization model
class DBAuthorization(BaseModel):
    db_type: DBType

# Postgres authorization
class PostgresAuth(DBAuthorization):
    db_type: Literal[DBType.POSTGRES] = DBType.POSTGRES
    host: str = Field(..., title="Database Host", description="The hostname or IP address of your PostgreSQL server")
    dbname: str = Field(..., title="Database Name", description="The name of the PostgreSQL database you want to connect to")
    user: str = Field(..., title="Username", description="Your PostgreSQL username for authentication")
    password: str = Field(..., title="Password", description="Your PostgreSQL password for authentication")
    port: int = Field(default=5432, ge=1, le=65535, title="Port Number", description="The port number where PostgreSQL is running")

# MySQL authorization  
class MySQLAuth(DBAuthorization):
    db_type: Literal[DBType.MYSQL] = DBType.MYSQL
    host: str = Field(..., title="Database Host", description="The hostname or IP address of your MySQL server")
    user: str = Field(..., title="Username", description="Your MySQL username for authentication")
    password: str = Field(..., title="Password", description="Your MySQL password for authentication")
    dbname: str = Field(..., title="Database Name", description="The name of the MySQL database you want to connect to")
    port: int = Field(default=3306, ge=1, le=65535, title="Port Number", description="The port number where MySQL is running")

# MSSQL authorization
class MSSQLAuth(DBAuthorization):
    db_type: Literal[DBType.MSSQL] = DBType.MSSQL
    odbc_conn_str: str = Field(..., title="ODBC Connection String", description="Complete ODBC connection string")

# SQLite authorization (file-based, no auth needed)
class SQLiteAuth(DBAuthorization):
    db_type: Literal[DBType.SQLITE] = DBType.SQLITE
    db_path: Path = Field(..., title="Database File Path", description="Full path to your local SQLite database file")

# DuckDB authorization (file-based or in-memory, no auth needed)
class DuckDBAuth(DBAuthorization):
    db_type: Literal[DBType.DUCKDB] = DBType.DUCKDB
    db_path: Optional[Path] = Field(default=None, title="Database File Path", description="Path to your local DuckDB file, or leave empty for an in-memory database")

# Snowflake authorization
class SnowflakeAuth(DBAuthorization):
    db_type: Literal[DBType.SNOWFLAKE] = DBType.SNOWFLAKE
    account: str = Field(..., title="Account Identifier", description="Your Snowflake account identifier")
    username: str = Field(..., title="Username", description="Your Snowflake username for authentication")
    password: str = Field(..., title="Password", description="Your Snowflake password for authentication")
    database: str = Field(..., title="Database Name", description="The name of the Snowflake database you want to connect to")
    role: str = Field(..., title="User Role", description="Your Snowflake role that determines your access permissions")

# BigQuery authorization
class BigQueryAuth(DBAuthorization):
    db_type: Literal[DBType.BIGQUERY] = DBType.BIGQUERY
    project_id: str = Field(..., title="Google Cloud Project ID", description="Your Google Cloud Platform project identifier")
    service_account_key_path: Optional[Path] = Field(default=None, title="Service Account Key File", description="Path to your Google Cloud service account JSON key file")
    service_account_key_json: Optional[str] = Field(default=None, title="Service Account Key JSON", description="Your Google Cloud service account key as a JSON string")

# Oracle authorization
class OracleAuth(DBAuthorization):
    db_type: Literal[DBType.ORACLE] = DBType.ORACLE
    host: str = Field(..., title="Database Host", description="The hostname or IP address of your Oracle database server")
    user: str = Field(..., title="Username", description="Your Oracle database username for authentication")
    password: str = Field(..., title="Password", description="Your Oracle database password for authentication")
    port: int = Field(default=1521, ge=1, le=65535, title="Port Number", description="The port number where Oracle is running")
    sid: Optional[str] = Field(default=None, title="Oracle SID", description="Oracle System Identifier - use either SID or service name, not both")
    service_name: Optional[str] = Field(default=None, title="Oracle Service Name", description="Oracle service name - use either service name or SID, not both")

# Generic authorization for other SQL databases
class OtherDBAuth(DBAuthorization):
    db_type: Literal[DBType.OTHER] = DBType.OTHER
    connection_params: dict = Field(..., title="Connection Parameters", description="Custom connection parameters specific to your database type")
    custom_connector_function: Optional[str] = Field(default=None, title="Custom Connector Function", description="Name of a custom function to handle database connections")

# Union type for all authorization models
DatabaseAuth = Union[
    PostgresAuth,
    MySQLAuth, 
    MSSQLAuth,
    SQLiteAuth,
    DuckDBAuth,
    SnowflakeAuth,
    BigQueryAuth,
    OracleAuth,
    OtherDBAuth
]

def validate_db_auth(data: dict) -> DatabaseAuth:
    if data.get("db_type") == DBType.POSTGRES:
        return PostgresAuth(**data)
    elif data.get("db_type") == DBType.MYSQL:
        return MySQLAuth(**data)
    elif data.get("db_type") == DBType.MSSQL:
        return MSSQLAuth(**data)
    elif data.get("db_type") == DBType.SQLITE:
        return SQLiteAuth(**data)
    elif data.get("db_type") == DBType.DUCKDB:
        return DuckDBAuth(**data)
    elif data.get("db_type") == DBType.SNOWFLAKE:
        return SnowflakeAuth(**data)
    elif data.get("db_type") == DBType.BIGQUERY:
        return BigQueryAuth(**data)
    elif data.get("db_type") == DBType.ORACLE:
        return OracleAuth(**data)
    else:
        return OtherDBAuth(**data)
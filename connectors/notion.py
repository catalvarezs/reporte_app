import os
from notion_client import Client


def fetch_notion_data(cliente: str) -> dict:
    token = os.environ["NOTION_TOKEN"]
    database_id = os.environ["NOTION_DATABASE_ID"]
    notion = Client(auth=token)

    response = notion.databases.query(
        database_id=database_id,
        filter={"property": "Cliente", "title": {"equals": cliente}},
    )
    return {"raw": response}

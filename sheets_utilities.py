"""
Google Sheets helpers for Mickey's marathon-plan spreadsheet.

The workbook (MARATHON_PLAN_SHEET_ID in .env) has two tabs:

  - "Philosophy": only cell A1 is used — the English description of the
    overall training philosophy for the current marathon.
  - "Current Marathon Plan": the week-by-week plan grid. Row 1 is the
    header (Week | Monday | ... | Sunday); column A holds the Monday
    date of each week; each day cell holds the planned workout, and —
    once the day has passed — the actual result plus a background color
    (green/yellow/red) applied via set_cell_background().

How auth works
--------------
Application Default Credentials, resolving to the per-agent SA on the
deployed Reasoning Engine and to your gcloud user locally. The sheet
must be shared (Editor) with whichever identity ADC resolves to — there
is no key file. The quota-project override matters: Workspace API calls
must bill to the agent's own project (where terraform enables
sheets.googleapis.com), not the Forum project the engine runs in.
"""
import os
from typing import Any, Dict, List, Optional

import google.auth
from google.auth.credentials import Credentials
from googleapiclient.discovery import build

from .secret_utilities import retry_on_transient_error


_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

# The red/yellow/green convention for plan compliance. "none" clears the
# background (used for planned-but-not-yet-done cells).
CELL_COLORS = {
    "green": {"red": 0.72, "green": 0.88, "blue": 0.80},
    "yellow": {"red": 1.0, "green": 0.90, "blue": 0.60},
    "red": {"red": 0.96, "green": 0.70, "blue": 0.68},
    "none": {"red": 1.0, "green": 1.0, "blue": 1.0},
}


class GoogleSheetsConnector:
    """Wrapper around the Google Sheets API for the plan workbook."""

    def __init__(self, credentials: Optional[Credentials] = None):
        if credentials is None:
            credentials, _ = google.auth.default(scopes=_SHEETS_SCOPES)
            agent_project = os.environ.get("AGENT_PROJECT_ID")
            if agent_project:
                credentials = credentials.with_quota_project(agent_project)
        self._credentials = credentials
        self._sheets_service = build("sheets", "v4", credentials=credentials)
        self._sheet_id_cache: Dict[str, int] = {}

    @retry_on_transient_error()
    def read_all(self, spreadsheet_id: str, cell_range: str) -> List[List[str]]:
        """Return every row in `cell_range` (e.g. "'Current Marathon Plan'!A:H")
        as lists of cell strings. Trailing empty cells are omitted by the API."""
        result = self._sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
        ).execute()
        return result.get("values", [])

    @retry_on_transient_error()
    def update_cells(
        self,
        spreadsheet_id: str,
        cell_range: str,
        values: List[List[Any]],
        value_input_option: str = "RAW",
    ) -> Dict[str, Any]:
        """Overwrite `cell_range` with `values` (list of rows). RAW by default
        so workout text starting with '=' or '+' isn't parsed as a formula."""
        return self._sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption=value_input_option,
            body={"values": values},
        ).execute()

    @retry_on_transient_error()
    def get_sheet_id_by_name(self, spreadsheet_id: str, sheet_name: str) -> int:
        """Resolve a tab name to its numeric sheetId (needed for formatting
        requests, which don't accept A1 notation). Cached per process."""
        cache_key = f"{spreadsheet_id}:{sheet_name}"
        if cache_key in self._sheet_id_cache:
            return self._sheet_id_cache[cache_key]
        meta = self._sheets_service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == sheet_name:
                self._sheet_id_cache[cache_key] = props["sheetId"]
                return props["sheetId"]
        raise ValueError(f"No tab named {sheet_name!r} in spreadsheet {spreadsheet_id}")

    @retry_on_transient_error()
    def set_cell_background(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        a1_cell: str,
        color: str,
    ) -> Dict[str, Any]:
        """Set one cell's background color.

        Args:
            sheet_name: Tab name, e.g. "Current Marathon Plan".
            a1_cell: Single cell in A1 notation WITHOUT the tab prefix, e.g. "C4".
            color: One of CELL_COLORS: "green", "yellow", "red", "none".
        """
        color_key = color.strip().lower()
        if color_key not in CELL_COLORS:
            raise ValueError(f"color must be one of {sorted(CELL_COLORS)}, got {color!r}")
        col_str = "".join(c for c in a1_cell if c.isalpha()).upper()
        row_str = "".join(c for c in a1_cell if c.isdigit())
        if not col_str or not row_str:
            raise ValueError(f"a1_cell must look like 'C4', got {a1_cell!r}")
        col_index = 0
        for ch in col_str:
            col_index = col_index * 26 + (ord(ch) - ord("A") + 1)
        col_index -= 1  # zero-based
        row_index = int(row_str) - 1

        sheet_id = self.get_sheet_id_by_name(spreadsheet_id, sheet_name)
        request = {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": CELL_COLORS[color_key]}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        }
        return self._sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [request]},
        ).execute()


# Process-wide cached connector, same pattern as get_docs_connector().
_connector: Optional[GoogleSheetsConnector] = None


def get_sheets_connector() -> GoogleSheetsConnector:
    """Return a process-wide cached `GoogleSheetsConnector`."""
    global _connector
    if _connector is None:
        _connector = GoogleSheetsConnector()
    return _connector

"""
Sheets Schema Reader - Custom Tool

Schema-first pristup za čitanje velikih Google Sheets tablica.
Prvo čita zaglavlja (prvi red), zatim koristi tu shemu za
optimizirano čitanje podataka.
"""

from typing import Dict, List, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class SheetsSchemaReader:
    """
    Schema-first reader za Google Sheets

    Prednosti:
    1. Smanjuje kontekstno korištenje - čita samo relevantne stupce
    2. Omogućava LLM-u da razumije strukturu prije nego čita podatke
    3. Podržava filtriranje i projekciju stupaca
    """

    def __init__(self, sheets_service):
        """
        Inicijalizacija schema reader-a

        Args:
            sheets_service: Google Sheets API service objekt
        """
        self.sheets_service = sheets_service

    def read_schema(
        self,
        spreadsheet_id: str,
        sheet_name: str = "Sheet1",
        header_row: int = 1
    ) -> Dict[str, int]:
        """
        Čita zaglavlja (schema) iz prvog reda tablice

        Args:
            spreadsheet_id: Spreadsheet ID
            sheet_name: Naziv sheeta
            header_row: Redni broj reda sa zaglavljima (default: 1)

        Returns:
            Dictionary {column_name: column_index}
        """
        # A1 notation za prvi red
        range_name = f"{sheet_name}!A{header_row}:ZZ{header_row}"

        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name
            ).execute()

            headers = result.get('values', [[]])[0]

            # Mapiranje naziv stupca -> indeks (0-based)
            schema = {
                header: idx
                for idx, header in enumerate(headers)
                if header  # Skip empty headers
            }

            logger.info(f"Read schema with {len(schema)} columns: {list(schema.keys())}")
            return schema

        except Exception as e:
            logger.error(f"Failed to read schema: {e}")
            raise

    def read_data_with_schema(
        self,
        spreadsheet_id: str,
        sheet_name: str = "Sheet1",
        schema: Optional[Dict[str, int]] = None,
        columns: Optional[List[str]] = None,
        max_rows: int = 100,
        start_row: int = 2
    ) -> List[Dict[str, Any]]:
        """
        Čita podatke koristeći schema (column projection)

        Args:
            spreadsheet_id: Spreadsheet ID
            sheet_name: Naziv sheeta
            schema: Schema dictionary (ako None, automatski se učita)
            columns: Lista stupaca za čitanje (projekcija). Ako None, čita sve.
            max_rows: Maksimalan broj redova za čitanje
            start_row: Početni red (default: 2, preskače header)

        Returns:
            Lista dictionary objekata {column_name: value}
        """
        # Učitaj schema ako nije proslijeđena
        if schema is None:
            schema = self.read_schema(spreadsheet_id, sheet_name)

        # Odredi koje stupce čitati
        if columns:
            # Filtriraj schema samo na tražene stupce
            selected_schema = {
                col: idx for col, idx in schema.items()
                if col in columns
            }
        else:
            selected_schema = schema

        if not selected_schema:
            logger.warning("No columns selected for reading")
            return []

        # Odredi A1 range
        # Nađi min i max column index
        col_indices = list(selected_schema.values())
        min_col = min(col_indices)
        max_col = max(col_indices)

        # Convert indices to letters (A, B, C, ...)
        start_col_letter = self._index_to_letter(min_col)
        end_col_letter = self._index_to_letter(max_col)

        end_row = start_row + max_rows - 1
        range_name = f"{sheet_name}!{start_col_letter}{start_row}:{end_col_letter}{end_row}"

        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name
            ).execute()

            rows = result.get('values', [])

            # Konvertiraj redove u dictionary objekte
            data = []
            for row in rows:
                row_dict = {}
                for col_name, col_idx in selected_schema.items():
                    # Adjust index relativno na start_col
                    adjusted_idx = col_idx - min_col
                    value = row[adjusted_idx] if adjusted_idx < len(row) else ''
                    row_dict[col_name] = value
                data.append(row_dict)

            logger.info(f"Read {len(data)} rows with {len(selected_schema)} columns")
            return data

        except Exception as e:
            logger.error(f"Failed to read data: {e}")
            raise

    def _index_to_letter(self, index: int) -> str:
        """
        Konvertira 0-based column index u A1 notation letter (A, B, ..., Z, AA, ...)

        Args:
            index: 0-based column index

        Returns:
            Column letter(s)
        """
        letter = ''
        while index >= 0:
            letter = chr(index % 26 + ord('A')) + letter
            index = index // 26 - 1
        return letter

    def get_column_summary(
        self,
        spreadsheet_id: str,
        sheet_name: str = "Sheet1",
        schema: Optional[Dict[str, int]] = None
    ) -> Dict[str, Any]:
        """
        Vraća sažetak o stupcima (korisno za LLM da odluči što čitati)

        Args:
            spreadsheet_id: Spreadsheet ID
            sheet_name: Naziv sheeta
            schema: Schema dictionary (ako None, automatski se učita)

        Returns:
            Dictionary sa sažetkom: {
                "columns": ["col1", "col2", ...],
                "column_count": N,
                "sample_values": {"col1": "value1", ...}
            }
        """
        if schema is None:
            schema = self.read_schema(spreadsheet_id, sheet_name)

        # Čitaj prvi red podataka (row 2) za sample values
        sample_data = self.read_data_with_schema(
            spreadsheet_id,
            sheet_name,
            schema=schema,
            max_rows=1,
            start_row=2
        )

        sample_values = sample_data[0] if sample_data else {}

        return {
            "columns": list(schema.keys()),
            "column_count": len(schema),
            "sample_values": sample_values
        }


def read_sheets_schema(spreadsheet_id: str, sheet_name: str = "Sheet1") -> Dict[str, int]:
    """
    Helper funkcija za čitanje schema

    Args:
        spreadsheet_id: Spreadsheet ID
        sheet_name: Naziv sheeta

    Returns:
        Schema dictionary
    """
    from googleapiclient.discovery import build
    from auth.credential_store import get_credential_store

    credentials = get_credential_store().get_credentials()
    service = build('sheets', 'v4', credentials=credentials)

    reader = SheetsSchemaReader(service)
    return reader.read_schema(spreadsheet_id, sheet_name)


# Function declaration za ADK
def get_sheets_schema_reader_tool():
    """
    Vraća Tool sa Sheets Schema Reader FunctionDeclaration

    Returns:
        Tool objekt
    """
    from google.genai.types import Tool, FunctionDeclaration

    return Tool(
        function_declarations=[
            FunctionDeclaration(
                name="read_sheets_schema",
                description=(
                    "Read column headers (schema) from a Google Sheets spreadsheet. "
                    "Use this BEFORE reading data to understand the structure. "
                    "Returns column names and their positions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {
                            "type": "string",
                            "description": "Spreadsheet ID from URL"
                        },
                        "sheet_name": {
                            "type": "string",
                            "description": "Sheet name (default: 'Sheet1')",
                            "default": "Sheet1"
                        }
                    },
                    "required": ["spreadsheet_id"]
                }
            )
        ]
    )

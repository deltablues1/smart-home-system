"""
Vision Handler
Implements OCR and image analysis logic using Gemini Flash.
"""

import os
import sys
import base64
import json
import logging
from typing import Optional, Dict, Any

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from google.genai import Client
from tools.schemas.receipt_schema import Receipt

logger = logging.getLogger(__name__)


def _own_party_rules() -> str:
    """Tell the model which party on the document is the user's own.

    A receipt names two parties, and the one printed on every document the
    household receives is the household itself. Without this, the model
    regularly extracts the recipient as the merchant. Configured, not
    hard-coded: OCR_OWN_NAME and OCR_OWN_TAX_ID in the environment.
    """
    name = os.getenv("OCR_OWN_NAME", "").strip()
    tax_id = os.getenv("OCR_OWN_TAX_ID", "").strip()
    if not name and not tax_id:
        return ""
    who = name or "the recipient"
    tax = f" (tax ID: {tax_id})" if tax_id else ""
    return f"""
            VENDOR vs RECIPIENT - PAY ATTENTION!
               - The RECIPIENT of this document is {who}{tax} - IGNORE THIS PARTY.
               - Extract the VENDOR: the party that ISSUED the document, usually at the top.
               - If merchant_name is "{who}" or tax_id is "{tax_id or '-'}", you picked
                 the wrong party - extract the OTHER one.
            """


class VisionHandler:
    """
    Handles Vision API interactions for OCR.
    """

    def __init__(self):
        """Initialize Vision Handler"""
        self.project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        self.location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        # Use standard flash model (thinking model not available in all regions)
        self.model = os.getenv("VISION_MODEL", "gemini-3.5-flash")

        try:
            self.client = Client(project=self.project_id, location=self.location)
            logger.info(f"VisionHandler initialized with model {self.model}")
        except Exception as e:
            logger.error(f"Failed to initialize VisionHandler client: {e}")
            self.client = None

    async def extract_receipt_data(self, image_data: str, mime_type: str = "image/jpeg", credentials: Optional[Any] = None, **kwargs) -> Dict[str, Any]:
        """
        Extracts structured data from a receipt image.
        Accepts base64 image data OR a Google Drive File ID.
        """
        if not self.client:
            raise RuntimeError("VisionHandler client not initialized")

        logger.info("Extracting receipt data...")

        try:
            # Check if image_data looks like a File ID (alphanumeric, no special chars like / or +, typical length ~33)
            # Base64 usually has / and + and ends with =. File IDs are url-safe base64 but usually just alphanumeric and -_
            is_file_id = len(image_data) < 100 and " " not in image_data and "/" not in image_data and "+" not in image_data

            if is_file_id:
                logger.info(f"Input looks like a File ID: {image_data}. Downloading...")
                from tools.api_implementations.drive_api import drive_get_file

                # Download file
                file_result = await drive_get_file(credentials, file_id=image_data, include_content=True)
                image_data = file_result.get('content')
                mime_type = file_result.get('metadata', {}).get('mimeType', mime_type)

                if not image_data:
                     raise ValueError(f"Failed to download content for file ID: {image_data}")

            # Handle BMP conversion (to save tokens)
            if mime_type == "image/bmp" or mime_type == "image/x-ms-bmp":
                logger.info("Detected BMP image. Converting to JPEG to reduce token usage...")
                try:
                    from PIL import Image
                    import io

                    # Decode base64
                    img_bytes = base64.b64decode(image_data)

                    # Open image
                    with Image.open(io.BytesIO(img_bytes)) as img:
                        # Convert to RGB (BMP can be RGBA or P)
                        if img.mode != 'RGB':
                            img = img.convert('RGB')

                        # Save as JPEG
                        output_buffer = io.BytesIO()
                        img.save(output_buffer, format='JPEG', quality=85)

                        # Encode back to base64
                        image_data = base64.b64encode(output_buffer.getvalue()).decode('utf-8')
                        mime_type = "image/jpeg"
                        logger.info("Successfully converted BMP to JPEG")

                except ImportError:
                    logger.warning("PIL not installed. Skipping BMP conversion. This might cause token limit errors.")
                except Exception as e:
                    logger.error(f"Error converting BMP: {e}. Proceeding with original image.")

            # Prepare the prompt
            prompt = f"""
            You are analyzing a RECEIPT or a received bill (račun).
            {_own_party_rules()}
            FIELDS TO EXTRACT:
               - merchant_name: the VENDOR's name (e.g., "Konzum d.o.o.", "INA d.d.")
               - transaction_date: document date in YYYY-MM-DD format
               - total_amount: total amount to pay (Ukupno)
               - currency: currency (EUR, HRK, USD)
               - receipt_number: document number (Broj računa)
               - tax_id: the VENDOR's tax ID (OIB)
               - tax_base: tax base amount (Osnovica)
               - tax_amount: VAT amount (Iznos PDV-a)
               - items: line items if visible
               - expense_category: category based on vendor (Hrana, Prijevoz, Ured, Režije, Ostalo)

            CONFIDENCE SCORING:
               - High confidence (>0.85) only if all critical fields are clearly visible
               - Low confidence (<0.7) if you had to guess or data is unclear

            Return the data as a JSON object matching the Receipt schema.
            """

            from google.genai import types

            # Create the image part (image_data is a base64 string)
            image_part = types.Part.from_bytes(
                data=base64.b64decode(image_data),
                mime_type=mime_type
            )

            # Call the model
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt, image_part],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": Receipt
                }
            )

            # Parse response
            if response.text:
                data = json.loads(response.text)
                logger.info("Receipt data extracted successfully")
                return data
            else:
                raise ValueError("Empty response from model")

        except Exception as e:
            logger.error(f"Error extracting receipt data: {e}")
            raise

    async def categorize_expense(self, merchant: str, items: list, total_amount: float, credentials: Optional[Any] = None, **kwargs) -> str:
        """
        Categorizes an expense based on details.
        """
        # Simple logic for now, could use LLM if needed
        # But since extract_receipt_data already does categorization, this might be redundant
        # or used for manual entries.

        prompt = f"Categorize expense: Merchant={merchant}, Items={items}, Amount={total_amount}"
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt
        )
        return response.text.strip()

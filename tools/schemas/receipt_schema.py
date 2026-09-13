"""
Receipt Data Schema - Pydantic Models

Optimized for Gemini Flash OCR with fail-safe design:
- Required fields: merchant_name, transaction_date, total_amount, expense_category
- Optional fields: items, VAT, payment_method (won't fail if missing)
"""

from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class LineItem(BaseModel):
    """Pojedinačna stavka na računu"""

    description: str = Field(..., description="Naziv artikla/usluge")
    quantity: Optional[float] = Field(None, description="Količina (ako postoji)")
    unit_price: Optional[float] = Field(None, description="Jedinična cijena")
    amount: float = Field(..., description="Ukupna cijena stavke")

    model_config = ConfigDict(
        extra="ignore"  # Allow extra fields (flexibility)
    )


class Receipt(BaseModel):
    """
    Kompletni podaci s računa - FAIL-SAFE design

    Kritična polja (required):
    - merchant_name, transaction_date, total_amount, expense_category

    Opciona polja (won't fail):
    - items, VAT, payment_method, receipt_number
    """

    # ========================================================================
    # REQUIRED FIELDS - Kritični podaci (mora postojati)
    # ========================================================================

    merchant_name: str = Field(
        ...,
        description="Naziv trgovine/dobavljača. Ako nije čitljivo, vrati 'Unknown'."
    )

    transaction_date: str = Field(
        ...,
        description="Datum transakcije u ISO 8601 formatu (YYYY-MM-DD). Ako nije vidljivo, procijeni na temelju slike."
    )

    total_amount: float = Field(
        ...,
        description="Ukupan iznos računa. MORA biti broj > 0."
    )

    currency: str = Field(
        default="EUR",
        description="Valuta (EUR, USD, HRK). Default: EUR."
    )

    expense_category: str = Field(
        ...,
        description=(
            "Kategorija troška. MORA biti JEDNA od: "
            "Hrana, Prijevoz, Ured, Režije, Ostalo. "
            "Kategoriziraj na temelju naziva trgovca i stavki."
        )
    )

    # ========================================================================
    # OPTIONAL FIELDS - Neće failati ako nedostaju
    # ========================================================================

    items: List[LineItem] = Field(
        default_factory=list,
        description=(
            "Lista stavki s računa. "
            "VAŽNO: Ako stavke nisu jasno vidljive ili čitljive, vrati PRAZNU LISTU []. "
            "Bolje je preskočiti nego griješiti."
        )
    )

    # Dodatni metapodaci (optional)
    receipt_number: Optional[str] = Field(
        None,
        description="Broj računa/fakture. Ako nije vidljivo, ostavi None."
    )

    payment_method: Optional[str] = Field(
        None,
        description="Način plaćanja: Gotovina, Kartica, Virman. Ako nije vidljivo, ostavi None."
    )

    vat_amount: Optional[float] = Field(
        None,
        description="Iznos PDV-a. Ako nije vidljivo, ostavi None."
    )

    # U-RA Specific Fields (Invoice Processing)
    invoice_number: Optional[str] = Field(
        None,
        description="Broj računa (npr. 123-1-1). Ako nije vidljivo, ostavi None."
    )

    tax_id: Optional[str] = Field(
        None,
        description="OIB dobavljača. Ako nije vidljivo, ostavi None."
    )

    tax_base: Optional[float] = Field(
        None,
        description="Porezna osnovica (iznos bez PDV-a). Ako nije vidljivo, ostavi None."
    )

    tax_amount: Optional[float] = Field(
        None,
        description="Iznos PDV-a. Ako nije vidljivo, ostavi None."
    )

    vat_rate: Optional[float] = Field(
        None,
        description="Stopa PDV-a (%). Ako nije vidljivo, ostavi None."
    )

    # ========================================================================
    # OCR METADATA
    # ========================================================================

    confidence_score: float = Field(
        ...,
        description=(
            "Pouzdanost ekstrakcije (0.0 - 1.0). "
            "VAŽNO za scoring:\n"
            "- Ako je slika mutna, odrezana ili tekst nečitljiv → confidence < 0.5\n"
            "- Ako nedostaju kritična polja ili su procijenjeni → confidence < 0.7\n"
            "- Ako je većina podataka jasna ali ima sitnih nedostataka → confidence 0.7-0.85\n"
            "- Samo ako je SVE savršeno čitljivo i ekstraktirano → confidence > 0.85"
        )
    )

    drive_file_id: Optional[str] = Field(
        None,
        description="ID slike računa na Google Drive-u (popunjava se naknadno)"
    )

    extraction_notes: Optional[str] = Field(
        None,
        description=(
            "Bilješke o ekstrakciji. "
            "Ako si nešto morao procijeniti ili pretpostaviti, napomeni ovdje."
        )
    )

    # ========================================================================
    # VALIDATORS
    # ========================================================================

    @field_validator('transaction_date')
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Validira ISO 8601 format datuma"""
        try:
            # Parse to validate
            datetime.fromisoformat(v)
            return v
        except ValueError:
            logger.warning(f"Invalid date format: {v}, attempting to fix")
            # Try common formats and convert
            for fmt in ["%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d"]:
                try:
                    dt = datetime.strptime(v, fmt)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue
            # Fallback: return as-is and log
            logger.error(f"Could not parse date: {v}")
            return v

    @field_validator('total_amount')
    @classmethod
    def validate_amount(cls, v: float) -> float:
        """Validira da je iznos pozitivan broj"""
        if v <= 0:
            raise ValueError(f"Total amount must be > 0, got {v}")
        return v

    @field_validator('expense_category')
    @classmethod
    def validate_category(cls, v: str) -> str:
        """Validira da je kategorija jedna od dozvoljenih"""
        valid_categories = ["Hrana", "Prijevoz", "Ured", "Režije", "Ostalo"]
        if v not in valid_categories:
            logger.warning(f"Invalid category: {v}, defaulting to 'Ostalo'")
            return "Ostalo"
        return v

    @field_validator('confidence_score')
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        """Validira da je confidence između 0 i 1"""
        if not 0.0 <= v <= 1.0:
            logger.warning(f"Confidence score {v} out of range, clamping to [0,1]")
            return max(0.0, min(1.0, v))
        return v

    model_config = ConfigDict(
        extra="ignore"  # Allow extra fields without failing
    )


class ReceiptValidation(BaseModel):
    """
    Rezultat validacije OCR ekstrakcije

    Koristi se za vraćanje rezultata s detaljnim feedback-om
    """

    success: bool = Field(
        ...,
        description="True ako su sva kritična polja uspješno ekstraktirana"
    )

    receipt: Optional[Receipt] = Field(
        None,
        description="Ekstraktirani podaci (None ako validation nije uspio)"
    )

    errors: List[str] = Field(
        default_factory=list,
        description="Lista kritičnih grešaka koje sprječavaju spremanje"
    )

    warnings: List[str] = Field(
        default_factory=list,
        description="Upozorenja o nedostajućim/sumnjivim podacima"
    )

    requires_manual_review: bool = Field(
        default=False,
        description="True ako treba manual review (confidence < 0.8 ili errors)"
    )

    model_config = ConfigDict(
        extra="ignore"
    )


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def validate_receipt_data(receipt: Receipt) -> ReceiptValidation:
    """
    Validira ekstraktirane podatke i vraća ReceiptValidation objekt

    Args:
        receipt: Receipt objekt iz OCR-a

    Returns:
        ReceiptValidation s detaljnim feedback-om
    """
    errors = []
    warnings = []

    # 1. Required fields check
    if not receipt.merchant_name or receipt.merchant_name.strip() == "Unknown":
        errors.append("Naziv trgovca nije ekstraktiran ili je 'Unknown'")

    if not receipt.transaction_date:
        errors.append("Datum nije ekstraktiran")

    if receipt.total_amount <= 0:
        errors.append(f"Nevaljani iznos: {receipt.total_amount}")

    # 2. Category validation (already done by Pydantic, but double-check)
    valid_categories = ["Hrana", "Prijevoz", "Ured", "Režije", "Ostalo"]
    if receipt.expense_category not in valid_categories:
        errors.append(f"Nepoznata kategorija: {receipt.expense_category}")

    # 3. Confidence check
    if receipt.confidence_score < 0.5:
        warnings.append("⚠️ Vrlo niska pouzdanost OCR-a (< 50%) - slika vjerojatno nečitljiva")
    elif receipt.confidence_score < 0.8:
        warnings.append("⚠️ Niska pouzdanost OCR-a (< 80%) - preporuča se manual review")

    # 4. Optional fields warnings (informational)
    if not receipt.items or len(receipt.items) == 0:
        warnings.append("Stavke nisu ekstraktirane (lista prazna)")

    if receipt.vat_amount is None:
        warnings.append("PDV nije ekstraktiran")

    if receipt.payment_method is None:
        warnings.append("Način plaćanja nije ekstraktiran")

    # 5. Date format check (already validated by Pydantic, but log)
    try:
        datetime.fromisoformat(receipt.transaction_date)
    except ValueError:
        errors.append(f"Nevaljani format datuma: {receipt.transaction_date}")

    # 6. Extraction notes check
    if receipt.extraction_notes:
        warnings.append(f"OCR napomena: {receipt.extraction_notes}")

    # 7. Determine if manual review needed
    requires_review = (
        len(errors) > 0 or
        receipt.confidence_score < 0.8
    )

    # 8. Build result
    return ReceiptValidation(
        success=len(errors) == 0,
        receipt=receipt if len(errors) == 0 else None,
        errors=errors,
        warnings=warnings,
        requires_manual_review=requires_review
    )


# ============================================================================
# EXAMPLE USAGE (for testing)
# ============================================================================

if __name__ == "__main__":
    print("=" * 80)
    print("Receipt Schema Test")
    print("=" * 80)

    # Test 1: Valid receipt (high confidence)
    print("\n1. Testing valid receipt (high confidence):")
    receipt_data = {
        "merchant_name": "Konzum",
        "transaction_date": "2025-11-27",
        "total_amount": 125.50,
        "currency": "EUR",
        "expense_category": "Hrana",
        "items": [
            {"description": "Mlijeko", "amount": 1.20},
            {"description": "Kruh", "amount": 0.90}
        ],
        "confidence_score": 0.95
    }

    receipt = Receipt(**receipt_data)
    validation = validate_receipt_data(receipt)

    print(f"   Success: {validation.success}")
    print(f"   Requires review: {validation.requires_manual_review}")
    print(f"   Warnings: {validation.warnings}")

    # Test 2: Low confidence receipt
    print("\n2. Testing low confidence receipt:")
    receipt_low = Receipt(
        merchant_name="INA",
        transaction_date="2025-11-26",
        total_amount=67.30,
        currency="EUR",
        expense_category="Prijevoz",
        confidence_score=0.65  # Low confidence
    )

    validation_low = validate_receipt_data(receipt_low)
    print(f"   Success: {validation_low.success}")
    print(f"   Requires review: {validation_low.requires_manual_review}")
    print(f"   Warnings: {validation_low.warnings}")

    # Test 3: Fail-safe (missing items - should NOT fail)
    print("\n3. Testing fail-safe (missing optional fields):")
    receipt_minimal = Receipt(
        merchant_name="Trgovina",
        transaction_date="2025-11-25",
        total_amount=50.00,
        currency="EUR",
        expense_category="Ostalo",
        items=[],  # Empty items - should NOT fail
        confidence_score=0.85
    )

    validation_minimal = validate_receipt_data(receipt_minimal)
    print(f"   Success: {validation_minimal.success}")
    print(f"   Warnings: {validation_minimal.warnings}")

    print("\n✅ Schema tests completed!")

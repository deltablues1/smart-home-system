"""
Vision API Implementation
Registers Vision tools with the ToolRegistry.
"""

import logging
from tools.handlers.vision_handler import VisionHandler

logger = logging.getLogger(__name__)

def register_vision_tools(tool_registry):
    """
    Registers Vision tools with the ToolRegistry.
    """
    try:
        handler = VisionHandler()
        
        # Register extract_receipt_data
        tool_registry.register_tool(
            name="extract_receipt_data",
            function=handler.extract_receipt_data,
            description="Extracts structured data from a receipt image.",
            parameters={
                "type": "object",
                "properties": {
                    "image_data": {
                        "type": "string",
                        "description": "Base64 encoded image data or Drive File ID"
                    },
                    "mime_type": {
                        "type": "string",
                        "description": "MIME type of the image (e.g., 'image/jpeg')"
                    }
                },
                "required": ["image_data"]
            }
        )
        
        # Register categorize_expense
        tool_registry.register_tool(
            name="categorize_expense",
            function=handler.categorize_expense,
            description="Categorizes an expense based on merchant and items.",
            parameters={
                "type": "object",
                "properties": {
                    "merchant": {
                        "type": "string",
                        "description": "Name of the merchant"
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of items purchased"
                    },
                    "total_amount": {
                        "type": "number",
                        "description": "Total amount of the expense"
                    }
                },
                "required": ["merchant", "total_amount"]
            }
        )
        
        logger.info("Vision tools registered successfully")
        
    except Exception as e:
        logger.error(f"Failed to register Vision tools: {e}")
        raise

"""
DeepSeek AI Client

Handles communication with DeepSeek API for semantic analysis of emails.
Extracts structured transaction data from unstructured email content.

Optimized for token reduction with:
- Compact system prompts
- Email content preprocessing
- JSON output mode
"""

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import get_settings
from app.core.exceptions import (
    DeepSeekAPIError,
    DeepSeekParseError,
    DeepSeekRateLimitError,
)
from app.core.logging import get_logger
from app.models.schemas import TransactionAnalysis, TransactionType

logger = get_logger(__name__)


# Compact system prompt optimized for token usage
SYSTEM_PROMPT_TRANSACTION = """Extrae transacción financiera del email. Responde JSON:

FORMATO:
{"amount":0.00,"currency":"USD","date":"YYYY-MM-DD","description":"","merchant":"","suggested_category":"","suggested_account_name":"","transaction_type":"withdrawal|deposit","confidence_score":0.0}

REGLAS:
- amount: positivo, decimal
- currency: ISO 4217 (USD,EUR,COP,MXN)
- date: YYYY-MM-DD
- transaction_type: withdrawal=gasto, deposit=ingreso
- suggested_category: Alimentación|Transporte|Entretenimiento|Servicios|Compras|Salud|Educación|Hogar|Transferencias
- suggested_account_name: IMPORTANTE - Para withdrawal, usa el NOMBRE DEL BANCO del remitente del email (ej: si viene de "notificaciones@lulobank.com" → "Lulo Bank", si viene de "alertas@bancolombia.com.co" → "Bancolombia"). NO uses nombres de tarjetas específicas como "Tarjeta débito 7556", usa solo el nombre del banco.
- confidence_score: 0.0-1.0

EJEMPLO INPUT:
De: notificaciones@lulobank.com
Compra aprobada $45.99 USD en AMAZON el 15/01/2024. Tarjeta ***1234

EJEMPLO OUTPUT:
{"amount":45.99,"currency":"USD","date":"2024-01-15","description":"Compra en Amazon","merchant":"Amazon","suggested_category":"Compras","suggested_account_name":"Lulo Bank","transaction_type":"withdrawal","confidence_score":0.95}"""


# System prompt for sender learning
SYSTEM_PROMPT_SENDER_LEARNING = """Analiza emails y extrae remitentes financieros. Responde JSON array.

FORMATO:
[{"keyword":"palabra_clave","sender_name":"Nombre Completo","sender_type":"bank|payment|store|subscription","is_financial":true,"confidence_score":0.9}]

TIPOS:
- bank: bancos, tarjetas de crédito
- payment: PayPal, Stripe, plataformas de pago
- store: tiendas online, comercios
- subscription: Netflix, Spotify, servicios

REGLAS:
- keyword: palabra única del email del remitente (ej: "lulo" de notificaciones@lulo.com.co)
- Solo incluir remitentes financieros (transacciones, compras, pagos)
- Excluir: marketing, newsletters, spam
- confidence_score: 0.0-1.0

EJEMPLO INPUT:
From: alertas@bancolombia.com.co - Compra aprobada
From: notificaciones@nequi.com - Transferencia recibida
From: newsletter@amazon.com - Ofertas del día

EJEMPLO OUTPUT:
[{"keyword":"bancolombia","sender_name":"Bancolombia","sender_type":"bank","is_financial":true,"confidence_score":0.95},{"keyword":"nequi","sender_name":"Nequi","sender_type":"payment","is_financial":true,"confidence_score":0.9}]"""


class DeepSeekClient:
    """
    Client for DeepSeek AI API.
    
    Analyzes email content and extracts structured transaction data
    using natural language processing with token optimization.
    """
    
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: httpx.AsyncClient | None = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.deepseek_timeout),
                headers={
                    "Authorization": f"Bearer {self.settings.deepseek_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
            )
        return self._client
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda retry_state: logger.warning(
            "deepseek_retry",
            attempt=retry_state.attempt_number,
        ),
    )
    async def _call_api(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 500,
    ) -> dict[str, Any]:
        """Make API call to DeepSeek with retry logic and JSON output mode."""
        client = await self._get_client()
        
        payload = {
            "model": self.settings.deepseek_model,
            "messages": messages,
            "temperature": 0.1,  # Low temperature for consistent extraction
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},  # Force JSON output
        }
        
        try:
            response = await client.post(
                self.settings.deepseek_api_url,
                json=payload,
            )
            
            if response.status_code == 429:
                raise DeepSeekRateLimitError(
                    "Rate limited by DeepSeek API",
                    details={"status_code": 429},
                )
            
            response.raise_for_status()
            return response.json()
            
        except httpx.HTTPStatusError as e:
            logger.error(
                "deepseek_api_error",
                status_code=e.response.status_code,
                response=e.response.text[:500],
            )
            raise DeepSeekAPIError(
                f"DeepSeek API error: {e.response.status_code}",
                details={"status_code": e.response.status_code},
                original_error=e,
            ) from e
    
    def _preprocess_email_content(self, content: str) -> str:
        """
        Preprocess email content to reduce tokens.
        
        Removes:
        - HTML tags
        - Email signatures
        - Legal footers
        - Excessive whitespace
        - URLs (keeps domain only)
        """
        # Remove HTML tags if present
        if self.settings.deepseek_strip_html:
            content = re.sub(r"<[^>]+>", " ", content)
        
        # Remove URLs, keep domain for context
        content = re.sub(
            r"https?://(?:www\.)?([^/\s]+)[^\s]*",
            r"[\1]",
            content
        )
        
        # Remove email signatures (common patterns)
        if self.settings.deepseek_strip_signatures:
            signature_patterns = [
                r"--\s*\n.*$",  # -- signature
                r"Enviado desde mi .*$",
                r"Sent from my .*$",
                r"Get Outlook for .*$",
            ]
            for pattern in signature_patterns:
                content = re.sub(pattern, "", content, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
        
        # Remove legal/marketing footers
        if self.settings.deepseek_strip_footers:
            footer_patterns = [
                r"Este mensaje es confidencial.*$",
                r"This email is confidential.*$",
                r"Para cancelar tu suscripción.*$",
                r"Unsubscribe.*$",
                r"©\s*\d{4}.*$",
                r"Todos los derechos reservados.*$",
                r"All rights reserved.*$",
                r"Política de privacidad.*$",
                r"Privacy policy.*$",
                r"Si no deseas recibir.*$",
            ]
            for pattern in footer_patterns:
                content = re.sub(pattern, "", content, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
        
        # Normalize whitespace
        content = re.sub(r"\s+", " ", content)
        content = re.sub(r"\n\s*\n+", "\n", content)
        
        # Remove common filler words/phrases (Spanish)
        filler_patterns = [
            r"\bEstimado\s+(cliente|usuario)\b",
            r"\bQuerido\s+(cliente|usuario)\b",
            r"\bLe\s+informamos\s+que\b",
            r"\bPor\s+medio\s+de\s+la\s+presente\b",
            r"\bA\s+continuación\b",
        ]
        for pattern in filler_patterns:
            content = re.sub(pattern, "", content, flags=re.IGNORECASE)
        
        # Truncate to max length
        max_chars = self.settings.deepseek_max_email_chars
        if len(content) > max_chars:
            # Try to cut at a sentence boundary
            truncated = content[:max_chars]
            last_period = truncated.rfind(".")
            if last_period > max_chars * 0.7:
                content = truncated[:last_period + 1]
            else:
                content = truncated + "..."
        
        return content.strip()
    
    async def analyze_email(
        self,
        email_content: str,
        email_subject: str = "",
        email_sender: str = "",
        preferred_currency: str = "USD",
    ) -> TransactionAnalysis:
        """
        Analyze email content and extract transaction data.
        
        Args:
            email_content: The email body (text or cleaned HTML).
            email_subject: Email subject for additional context.
            email_sender: Email sender for context.
            preferred_currency: Default currency if not detected.
            
        Returns:
            TransactionAnalysis with extracted data.
            
        Raises:
            DeepSeekAPIError: If API call fails.
            DeepSeekParseError: If response parsing fails.
        """
        # Preprocess content to reduce tokens
        processed_content = self._preprocess_email_content(email_content)
        
        # Build compact user message
        user_content = self._build_compact_message(
            processed_content,
            email_subject,
            email_sender,
            preferred_currency,
        )
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_TRANSACTION},
            {"role": "user", "content": user_content},
        ]
        
        logger.info(
            "deepseek_analyzing_email",
            subject=email_subject[:30] if email_subject else "N/A",
            sender=email_sender[:50] if email_sender else "N/A",
            content_chars=len(processed_content),
        )
        
        try:
            response = await self._call_api(messages, max_tokens=300)
            return self._parse_transaction_response(
                response, preferred_currency, email_sender
            )
            
        except (DeepSeekAPIError, DeepSeekRateLimitError):
            raise
        except Exception as e:
            logger.error("deepseek_analysis_failed", error=str(e))
            raise DeepSeekParseError(
                "Failed to analyze email content",
                original_error=e,
            ) from e
    
    def _build_compact_message(
        self,
        content: str,
        subject: str,
        sender: str,
        currency: str,
    ) -> str:
        """Build compact user message for minimal tokens."""
        parts = []
        
        # Only add non-empty fields
        if sender:
            parts.append(f"De:{sender}")
        if subject:
            parts.append(f"Asunto:{subject}")
        
        parts.append(f"Moneda:{currency}")
        parts.append(f"Hoy:{datetime.utcnow().strftime('%Y-%m-%d')}")
        parts.append("")
        parts.append(content)
        
        return "\n".join(parts)
    
    def _parse_transaction_response(
        self,
        response: dict[str, Any],
        default_currency: str,
        email_sender: str = "",
    ) -> TransactionAnalysis:
        """Parse API response into TransactionAnalysis."""
        try:
            # Extract content from response
            choices = response.get("choices", [])
            if not choices:
                raise DeepSeekParseError("Empty response from DeepSeek")
            
            content = choices[0].get("message", {}).get("content", "")
            
            # Parse JSON directly (JSON mode ensures valid JSON)
            data = json.loads(content)
            
            # Build TransactionAnalysis with validation and defaults
            return TransactionAnalysis(
                amount=self._parse_amount(data.get("amount", 0)),
                currency=data.get("currency", default_currency).upper(),
                date=self._parse_date(data.get("date")),
                description=data.get("description", "Transacción sin descripción"),
                merchant=data.get("merchant", ""),
                suggested_category=data.get("suggested_category", "Sin Categoría"),
                suggested_account_name=data.get("suggested_account_name", ""),
                transaction_type=self._parse_transaction_type(
                    data.get("transaction_type", "withdrawal")
                ),
                confidence_score=float(data.get("confidence_score", 0.5)),
                raw_extracted=data,
                email_sender=email_sender,
            )
            
        except json.JSONDecodeError as e:
            logger.error("deepseek_json_parse_failed", error=str(e), content=content[:200])
            raise DeepSeekParseError(
                "Failed to parse JSON response",
                details={"content": content[:200]},
                original_error=e,
            ) from e
        except Exception as e:
            logger.error("deepseek_parse_failed", error=str(e))
            raise DeepSeekParseError(
                "Failed to parse DeepSeek response",
                original_error=e,
            ) from e
    
    async def analyze_senders_for_learning(
        self,
        email_summaries: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """
        Analyze a batch of emails to learn new financial senders.
        
        Args:
            email_summaries: List of dicts with 'sender' and 'subject' keys.
            
        Returns:
            List of identified financial senders.
        """
        # Build compact input
        sender_lines = []
        for email in email_summaries[:50]:  # Limit to 50 for token efficiency
            sender = email.get("sender", "")
            subject = email.get("subject", "")[:50]  # Truncate subject
            sender_lines.append(f"From: {sender} - {subject}")
        
        user_content = "\n".join(sender_lines)
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_SENDER_LEARNING},
            {"role": "user", "content": user_content},
        ]
        
        logger.info(
            "deepseek_learning_senders",
            email_count=len(email_summaries),
        )
        
        try:
            response = await self._call_api(messages, max_tokens=800)
            return self._parse_sender_learning_response(response)
            
        except Exception as e:
            logger.error("deepseek_sender_learning_failed", error=str(e))
            return []
    
    def _parse_sender_learning_response(
        self,
        response: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Parse sender learning response."""
        try:
            choices = response.get("choices", [])
            if not choices:
                return []
            
            content = choices[0].get("message", {}).get("content", "")
            data = json.loads(content)
            
            # Ensure data is a list
            if not isinstance(data, list):
                data = [data] if isinstance(data, dict) else []
            
            # Filter only financial senders with good confidence
            financial_senders = [
                sender for sender in data
                if sender.get("is_financial", False) 
                and sender.get("confidence_score", 0) >= 0.7
            ]
            
            return financial_senders
            
        except Exception as e:
            logger.error("deepseek_parse_senders_failed", error=str(e))
            return []
    
    def _parse_amount(self, value: Any) -> Decimal:
        """Parse amount from various formats."""
        if isinstance(value, (int, float)):
            return Decimal(str(abs(value)))
        
        if isinstance(value, str):
            # Remove currency symbols and normalize
            cleaned = re.sub(r"[^\d.,\-]", "", value)
            cleaned = cleaned.replace(",", ".")
            
            # Handle negative amounts (make positive)
            try:
                return abs(Decimal(cleaned))
            except Exception:
                return Decimal("0")
        
        return Decimal("0")
    
    def _parse_date(self, value: Any) -> datetime:
        """Parse date from various formats."""
        if isinstance(value, datetime):
            return value
        
        if isinstance(value, str):
            # Try common formats
            formats = [
                "%Y-%m-%d",
                "%Y/%m/%d",
                "%d-%m-%Y",
                "%d/%m/%Y",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ",
            ]
            
            for fmt in formats:
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
        
        # Default to today
        return datetime.utcnow()
    
    def _parse_transaction_type(self, value: str) -> TransactionType:
        """Parse transaction type from string."""
        value_lower = value.lower().strip()
        
        if value_lower in ("deposit", "income", "ingreso", "abono"):
            return TransactionType.DEPOSIT
        
        if value_lower in ("transfer", "transferencia"):
            return TransactionType.TRANSFER
        
        # Default to withdrawal for expenses
        return TransactionType.WITHDRAWAL
    
    async def check_connection(self) -> bool:
        """Check if DeepSeek API is accessible."""
        try:
            # Simple test message
            messages = [
                {"role": "system", "content": "Respond JSON: {\"status\": \"ok\"}"},
                {"role": "user", "content": "Test"},
            ]
            
            response = await self._call_api(messages, max_tokens=20)
            return bool(response.get("choices"))
            
        except Exception as e:
            logger.error("deepseek_connection_failed", error=str(e))
            return False

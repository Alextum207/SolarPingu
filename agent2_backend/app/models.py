from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StringEnum(str, Enum):
    """Enum that serializes cleanly as a JSON string."""


class OwnerStatus(StringEnum):
    OWNER = "owner"
    CO_OWNER = "co_owner"
    FAMILY_OWNER = "family_owner"
    PROPERTY_MANAGER = "property_manager"
    RENTER = "renter"
    UNKNOWN = "unknown"


class InstallationTimeline(StringEnum):
    IMMEDIATELY = "immediately"
    WITHIN_1_MONTH = "within_1_month"
    WITHIN_3_MONTHS = "within_3_months"
    WITHIN_6_MONTHS = "within_6_months"
    THIS_YEAR = "this_year"
    EXPLORING = "exploring"
    UNKNOWN = "unknown"


class RoofSuitability(StringEnum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    MODERATE = "MODERATE"
    POOR = "POOR"


class GeocodeDataSource(StringEnum):
    GOOGLE_GEOCODING_API = "GOOGLE_GEOCODING_API"
    MOCK = "MOCK"


class SolarDataSource(StringEnum):
    GOOGLE_SOLAR_API = "GOOGLE_SOLAR_API"
    MOCK = "MOCK"


class RoofImageSource(StringEnum):
    GOOGLE_MAPS_STATIC = "GOOGLE_MAPS_STATIC"
    UNAVAILABLE = "UNAVAILABLE"


class BusinessLeadSource(StringEnum):
    GOOGLE_PLACES = "GOOGLE_PLACES"
    MOCK = "MOCK"


class Agent1DeliveryStatus(StringEnum):
    SENT = "SENT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class Decision(StringEnum):
    PURSUE = "PURSUE"
    NURTURE = "NURTURE"
    REJECT = "REJECT"


class ResourceLevel(StringEnum):
    HIGH_TOUCH = "HIGH_TOUCH"
    MEDIUM_TOUCH = "MEDIUM_TOUCH"
    LOW_TOUCH = "LOW_TOUCH"


class NextAction(StringEnum):
    GENERATE_OFFER_AND_SCHEDULE_CONSULTATION = (
        "GENERATE_OFFER_AND_SCHEDULE_CONSULTATION"
    )
    SEND_FINANCING_INFO_AND_FOLLOW_UP = "SEND_FINANCING_INFO_AND_FOLLOW_UP"
    SEND_EDUCATIONAL_CONTENT_AND_RECHECK = "SEND_EDUCATIONAL_CONTENT_AND_RECHECK"
    QUALIFY_OWNER_STATUS_BEFORE_NEXT_STEP = "QUALIFY_OWNER_STATUS_BEFORE_NEXT_STEP"
    CLOSE_OUT_NOT_A_FIT = "CLOSE_OUT_NOT_A_FIT"


class BaseApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        use_enum_values=False,
    )


class AddressEvaluationRequest(BaseApiModel):
    address: str = Field(..., min_length=3, max_length=240)
    leadId: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    batteryInterest: bool = False
    budgetRange: str = Field(default="unknown", min_length=2, max_length=40)
    installationTimeline: InstallationTimeline = InstallationTimeline.UNKNOWN
    ownerStatus: OwnerStatus = OwnerStatus.UNKNOWN
    objections: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("leadId", "name", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("budgetRange", mode="before")
    @classmethod
    def validate_budget_range(cls, value: Any) -> str:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "unknown"

        normalized = str(value).strip().lower().replace(" ", "_")
        if normalized in {"unknown", "not_discussed", "notdiscussed"}:
            return "unknown"
        if not any(char.isdigit() for char in normalized):
            raise ValueError(
                "budgetRange must include a numeric range, such as '15000-20000', "
                "or be 'unknown'"
            )
        return normalized

    @field_validator("objections", mode="before")
    @classmethod
    def normalize_objections(cls, values: Any) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("objections must be a list of strings")
        return [str(value).strip().lower() for value in values if str(value).strip()]

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "leadId": "L-001",
                "address": "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
                "name": "Anna Becker",
                "batteryInterest": True,
                "budgetRange": "15000-20000",
                "installationTimeline": "within_3_months",
                "ownerStatus": "owner",
                "objections": ["financing uncertainty"],
            }
        },
    )


class BusinessSearchRequest(BaseApiModel):
    city: str = Field(..., min_length=2, max_length=120)

    @field_validator("city")
    @classmethod
    def city_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("city must not be blank")
        return normalized

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"example": {"city": "Frankfurt am Main"}},
    )


class BusinessCandidate(BaseApiModel):
    placeId: str
    businessName: str
    category: str
    address: str
    phone: str | None = None
    website: str | None = None
    googleMapsUrl: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    source: BusinessLeadSource = BusinessLeadSource.GOOGLE_PLACES


class GeocodeResult(BaseApiModel):
    inputAddress: str
    formattedAddress: str
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    placeId: str | None = None
    dataSource: GeocodeDataSource = GeocodeDataSource.MOCK
    fallbackReason: str | None = None


class SolarPotential(BaseApiModel):
    maxPanels: int = Field(..., ge=0)
    panelCapacityWatts: float = Field(..., ge=0)
    panelWidthMeters: float = Field(default=1.134, gt=0)
    panelHeightMeters: float = Field(default=1.722, gt=0)
    maxSunshineHoursPerYear: float = Field(..., ge=0)
    estimatedKwPeak: float = Field(..., ge=0)
    yearlyEnergyKwh: int = Field(..., ge=0)
    roofOrientationScore: float = Field(..., ge=0, le=1)
    roofPitchScore: float = Field(..., ge=0, le=1)
    roofSuitability: RoofSuitability
    dataSource: SolarDataSource = SolarDataSource.MOCK
    imageryQuality: str | None = None
    fallbackReason: str | None = None


class RoofVisualization(BaseApiModel):
    roofImageUrl: str | None = None
    roofImageSource: RoofImageSource
    imageryDate: str | None = None
    imageWarning: str | None = None


class VisionAnalysis(BaseApiModel):
    visualSolarPotentialScore: float = Field(..., ge=0, le=1)
    roofType: str = Field(default="unknown", min_length=1, max_length=80)
    blockers: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0, ge=0, le=1)
    warning: str | None = Field(default=None, max_length=300)

    @field_validator("blockers", mode="before")
    @classmethod
    def normalize_blockers(cls, values: Any) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            return [str(values).strip()[:80]] if str(values).strip() else []
        return [str(value).strip()[:80] for value in values if str(value).strip()]


class FinderSolarSummary(BaseApiModel):
    estimatedKwPeak: float = Field(..., ge=0)
    yearlyEnergyKwh: int = Field(..., ge=0)
    panelCount: int = Field(..., ge=0)
    profitabilityScore: float = Field(..., ge=0, le=1)
    decision: Decision


class Agent1DeliveryResult(BaseApiModel):
    status: Agent1DeliveryStatus
    sent: bool = False
    statusCode: int | None = None
    warning: str | None = Field(default=None, max_length=300)


class FinderTraceEvent(BaseApiModel):
    step: str = Field(..., min_length=1, max_length=80)
    tool: str = Field(..., min_length=1, max_length=80)
    status: str = Field(..., min_length=1, max_length=40)
    thought: str = Field(..., min_length=1, max_length=300)
    address: str | None = Field(default=None, max_length=260)
    businessName: str | None = Field(default=None, max_length=160)
    detail: str | None = Field(default=None, max_length=500)


class FinderLead(BaseApiModel):
    leadId: str
    source: BusinessLeadSource
    businessName: str
    category: str
    address: str
    phone: str | None = None
    website: str | None = None
    googleMapsUrl: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    roofImageUrl: str | None = None
    solar: FinderSolarSummary
    vision: VisionAnalysis
    qualified: bool
    qualificationReason: str
    sentToAgent1: bool
    agent1Status: Agent1DeliveryStatus
    agent1Warning: str | None = None


class FinderRunResponse(BaseApiModel):
    runId: str
    city: str
    discoveredCount: int = Field(..., ge=0)
    qualifiedCount: int = Field(..., ge=0)
    sentToAgent1Count: int = Field(..., ge=0)
    trace: list[FinderTraceEvent] = Field(default_factory=list)
    leads: list[FinderLead] = Field(default_factory=list)


class FinancialEstimate(BaseApiModel):
    estimatedPriceMin: int = Field(..., ge=0)
    estimatedPriceMax: int = Field(..., ge=0)
    annualSavingsEstimate: int = Field(..., ge=0)
    paybackYears: float = Field(..., ge=0)


class LeadScores(BaseApiModel):
    leadFitScore: float = Field(..., ge=0, le=1)
    profitabilityScore: float = Field(..., ge=0, le=1)
    ghostingRiskScore: float = Field(..., ge=0, le=1)


class DecisionLayerResult(BaseApiModel):
    decision: Decision
    resourceLevel: ResourceLevel
    nextAction: NextAction
    assignedRep: str = Field(..., min_length=1, max_length=80)
    reasoning: str = Field(..., min_length=1, max_length=360)


class ProjectDecision(BaseApiModel):
    leadId: str
    inputAddress: str
    formattedAddress: str
    latitude: float
    longitude: float
    placeId: str | None = None
    geocodeSource: GeocodeDataSource
    solarSource: SolarDataSource
    fallbackWarning: str | None = None
    roofImageUrl: str | None = None
    roofImageSource: RoofImageSource
    imageryDate: str | None = None
    imageWarning: str | None = None
    panelCount: int = Field(..., ge=0)
    panelCapacityWatts: float = Field(..., ge=0)
    estimatedKwPeak: float = Field(..., ge=0)
    yearlyEnergyKwh: int = Field(..., ge=0)
    roofOrientationScore: float = Field(..., ge=0, le=1)
    roofPitchScore: float = Field(..., ge=0, le=1)
    annualSavingsEstimate: int = Field(..., ge=0)
    paybackYears: float = Field(..., ge=0)
    estimatedPriceMin: int = Field(..., ge=0)
    estimatedPriceMax: int = Field(..., ge=0)
    leadFitScore: float = Field(..., ge=0, le=1)
    profitabilityScore: float = Field(..., ge=0, le=1)
    ghostingRiskScore: float = Field(..., ge=0, le=1)
    decision: Decision
    resourceLevel: ResourceLevel
    nextAction: NextAction
    assignedRep: str = Field(..., min_length=1, max_length=80)
    reasoning: str = Field(..., min_length=1, max_length=360)

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "leadId": "L-001",
                "inputAddress": (
                    "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany"
                ),
                "formattedAddress": (
                    "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany"
                ),
                "latitude": 50.160936,
                "longitude": 8.486981,
                "placeId": "ChIJn-S8N4imvUcRGloQiGcQfCg",
                "geocodeSource": "GOOGLE_GEOCODING_API",
                "solarSource": "GOOGLE_SOLAR_API",
                "fallbackWarning": None,
                "roofImageUrl": "/agent2/roof-image/L-001-f50b0b93c3.png",
                "roofImageSource": "GOOGLE_MAPS_STATIC",
                "imageryDate": None,
                "imageWarning": None,
                "panelCount": 29,
                "panelCapacityWatts": 400,
                "estimatedKwPeak": 11.6,
                "yearlyEnergyKwh": 10675,
                "roofOrientationScore": 0.56,
                "roofPitchScore": 0.93,
                "annualSavingsEstimate": 2380,
                "paybackYears": 10.0,
                "estimatedPriceMin": 20800,
                "estimatedPriceMax": 26700,
                "leadFitScore": 0.84,
                "profitabilityScore": 0.82,
                "ghostingRiskScore": 0.27,
                "decision": "PURSUE",
                "resourceLevel": "MEDIUM_TOUCH",
                "nextAction": "GENERATE_OFFER_AND_SCHEDULE_CONSULTATION",
                "assignedRep": "Sales Rep 1",
                "reasoning": (
                    "Strong roof potential, good estimated annual energy, "
                    "owner-occupied property, and short buying timeline."
                ),
            }
        },
    )


class HealthResponse(BaseApiModel):
    status: str
    service: str
    version: str
    mockGeocodingEnabled: bool
    mockSolarEnabled: bool
    mockPlacesEnabled: bool
    geminiConfigured: bool
    featherlessConfigured: bool
    agent1WebhookConfigured: bool


def model_to_jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")

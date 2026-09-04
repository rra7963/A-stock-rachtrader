"""Closed schema tests for the batch event-portfolio API."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from tradingagents.api.portfolio_schemas import (
    AgentEventPortfolioAllocation,
    EventPortfolioPlanRequest,
    EventPortfolioPlanResponse,
)


def valid_portfolio_request_payload() -> dict:
    return {
        "schema_version": "1.0",
        "request_id": "rachel_executor:portfolio:2026-08-17",
        "requested_at": "2026-08-17T08:31:00+08:00",
        "decision_deadline": "2026-08-17T14:00:00+08:00",
        "event_window": {
            "starts_at": "2026-08-14T08:30:00+08:00",
            "ends_at": "2026-08-17T08:30:00+08:00",
            "coverage_status": "partial",
            "coverage_gap_count": 1,
        },
        "events": [
            {
                "event_id": "event-1",
                "signal_level": "S",
                "title": "Material order announcement",
                "summary": "The company disclosed a material customer order.",
                "source": "event-sync-full",
                "published_at": "2026-08-16T10:00:00+08:00",
                "received_at": "2026-08-16T10:01:00+08:00",
            }
        ],
        "instruments": [
            {
                "stock_code": "000001",
                "stock_name": "Existing Holding",
                "currently_held": True,
                "current_quantity": 1000,
                "sellable_quantity": 1000,
                "confirmed_cost": "9.50",
                "reference_price": "10.00",
                "upper_limit_price": "11.00",
                "lower_limit_price": "9.00",
                "quote_at": "2026-08-17T08:30:30+08:00",
                "current_weight_percent": "20.00",
                "event_links": [],
            },
            {
                "stock_code": "600000",
                "stock_name": "New Candidate",
                "currently_held": False,
                "current_quantity": 0,
                "sellable_quantity": None,
                "confirmed_cost": None,
                "reference_price": "10.20",
                "upper_limit_price": "11.22",
                "lower_limit_price": "9.18",
                "quote_at": "2026-08-17T08:30:30+08:00",
                "current_weight_percent": "0.00",
                "event_links": [
                    {
                        "event_id": "event-1",
                        "relevance": "high",
                        "relevance_reason": "Directly named in the order announcement.",
                    }
                ],
            },
        ],
        "portfolio": {
            "available_cash_cny": "40000.00",
            "total_equity_cny": "50000.00",
        },
    }


def test_valid_request_accepts_held_instrument_without_current_window_event() -> None:
    request = EventPortfolioPlanRequest.model_validate(valid_portfolio_request_payload())

    assert request.instruments[0].currently_held is True
    assert request.instruments[0].event_links == []
    assert request.event_window.coverage_status == "partial"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("events", 0, "signal_level"), "A"),
        (("instruments", 1, "stock_code"), "688001"),
        (("instruments", 1, "stock_code"), "689001"),
    ],
)
def test_schema_1_0_rejects_v1_1_event_and_market_features(
    path: tuple,
    value: object,
) -> None:
    payload = valid_portfolio_request_payload()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(payload)


def test_schema_1_1_accepts_mixed_signal_levels_and_star_market_instruments() -> None:
    payload = valid_portfolio_request_payload()
    payload["schema_version"] = "1.1"
    payload["events"].append(
        {
            **deepcopy(payload["events"][0]),
            "event_id": "event-a",
            "signal_level": "A",
        }
    )
    payload["instruments"][1]["stock_code"] = "688001"
    payload["instruments"][1]["event_links"].append(
        {
            "event_id": "event-a",
            "relevance": "high",
            "relevance_reason": "The A event directly names the STAR Market issuer.",
        }
    )

    request = EventPortfolioPlanRequest.model_validate(payload)

    assert [event.signal_level for event in request.events] == ["S", "A"]
    assert request.instruments[1].stock_code == "688001"


@pytest.mark.parametrize("signal_level", ["B", "S级", "A级", "s", "a"])
def test_signal_level_remains_exact_and_closed(signal_level: str) -> None:
    payload = valid_portfolio_request_payload()
    payload["schema_version"] = "1.1"
    payload["events"][0]["signal_level"] = signal_level

    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("portfolio", "available_cash_cny"), 40000.0),
        (("instruments", 0, "reference_price"), 10.0),
        (("instruments", 0, "current_weight_percent"), 20.0),
    ],
)
def test_decimal_wire_values_must_be_strings(path: tuple, value: object) -> None:
    payload = valid_portfolio_request_payload()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(payload)


def test_window_coverage_and_half_open_receipt_are_closed() -> None:
    complete_with_gap = valid_portfolio_request_payload()
    complete_with_gap["event_window"]["coverage_status"] = "complete"
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(complete_with_gap)

    boundary = valid_portfolio_request_payload()
    boundary["events"][0]["received_at"] = boundary["event_window"]["ends_at"]
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(boundary)


def test_membership_links_and_held_first_are_closed() -> None:
    unknown_link = valid_portfolio_request_payload()
    unknown_link["instruments"][1]["event_links"][0]["event_id"] = "missing"
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(unknown_link)

    reordered = valid_portfolio_request_payload()
    reordered["instruments"].reverse()
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(reordered)

    no_provenance = valid_portfolio_request_payload()
    no_provenance["instruments"][1]["event_links"] = []
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(no_provenance)


def test_unknown_fields_and_duplicate_members_are_rejected() -> None:
    unknown = valid_portfolio_request_payload()
    unknown["account_id"] = "must-not-be-accepted"
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(unknown)

    duplicate = valid_portfolio_request_payload()
    duplicate["instruments"].append(deepcopy(duplicate["instruments"][1]))
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(duplicate)


def test_duplicate_events_links_and_nonheld_holding_facts_are_rejected() -> None:
    duplicate_event = valid_portfolio_request_payload()
    duplicate_event["events"].append(deepcopy(duplicate_event["events"][0]))
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(duplicate_event)

    duplicate_link = valid_portfolio_request_payload()
    duplicate_link["instruments"][1]["event_links"].append(
        deepcopy(duplicate_link["instruments"][1]["event_links"][0])
    )
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(duplicate_link)

    nonheld_weight = valid_portfolio_request_payload()
    nonheld_weight["instruments"][1]["current_weight_percent"] = "0.01"
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(nonheld_weight)


def test_request_accepts_twenty_instruments_but_rejects_twenty_one() -> None:
    payload = valid_portfolio_request_payload()
    template = payload["instruments"][1]
    payload["instruments"] = [payload["instruments"][0]] + [
        {**deepcopy(template), "stock_code": f"600{index:03d}"} for index in range(19)
    ]

    request = EventPortfolioPlanRequest.model_validate(payload)
    assert len(request.instruments) == 20

    too_many = deepcopy(payload)
    too_many["instruments"].append({**deepcopy(template), "stock_code": "601999"})
    with pytest.raises(ValidationError):
        EventPortfolioPlanRequest.model_validate(too_many)


def test_agent_allocation_requires_exact_100_percent() -> None:
    valid = {
        "positions": [
            {
                "stock_code": "000001",
                "target_weight_percent": "70.00",
                "rating": "Hold",
                "rationale": "Retain a bounded core holding.",
            },
            {
                "stock_code": "600000",
                "target_weight_percent": "20.00",
                "rating": "Buy",
                "rationale": "The new event has stronger relative conviction.",
            },
        ],
        "cash_weight_percent": "10.00",
        "portfolio_summary": "Maintain a diversified long-only portfolio.",
        "risk_note": "The event feed was partially covered.",
    }
    allocation = AgentEventPortfolioAllocation.model_validate(valid)
    assert str(allocation.cash_weight_percent) == "10.00"

    invalid = deepcopy(valid)
    invalid["cash_weight_percent"] = "9.99"
    with pytest.raises(ValidationError):
        AgentEventPortfolioAllocation.model_validate(invalid)


def test_response_rejects_duplicate_positions() -> None:
    response = {
        "schema_version": "1.0",
        "request_id": "rachel_executor:portfolio:2026-08-17",
        "request_sha256": "a" * 64,
        "decision_id": "b" * 64,
        "decided_at": "2026-08-17T09:00:00+08:00",
        "reason_code": "approved",
        "positions": [
            {
                "stock_code": "000001",
                "target_weight_percent": "40.00",
                "rating": "Hold",
                "rationale": "Retain the existing holding.",
            },
            {
                "stock_code": "000001",
                "target_weight_percent": "40.00",
                "rating": "Hold",
                "rationale": "Duplicate must fail closed.",
            },
        ],
        "cash_weight_percent": "20.00",
        "portfolio_summary": "Invalid duplicate membership.",
        "risk_note": "Duplicate membership is unsafe.",
        "provenance": {
            "llm_provider": "fake",
            "deep_model": "fake-deep",
            "quick_model": "fake-quick",
        },
    }
    with pytest.raises(ValidationError):
        EventPortfolioPlanResponse.model_validate(response)


def test_schema_1_1_response_accepts_star_market_membership() -> None:
    response = {
        "schema_version": "1.1",
        "request_id": "rachel_executor:portfolio:2026-08-17",
        "request_sha256": "a" * 64,
        "decision_id": "b" * 64,
        "decided_at": "2026-08-17T09:00:00+08:00",
        "reason_code": "approved",
        "positions": [
            {
                "stock_code": "689001",
                "target_weight_percent": "100.00",
                "rating": "Buy",
                "rationale": "The request explicitly opted into portfolio schema 1.1.",
            }
        ],
        "cash_weight_percent": "0.00",
        "portfolio_summary": "One-member STAR Market portfolio.",
        "risk_note": "Concentrated allocation risk.",
        "provenance": {
            "llm_provider": "fake",
            "deep_model": "fake-deep",
            "quick_model": "fake-quick",
        },
    }

    validated = EventPortfolioPlanResponse.model_validate(response)

    assert validated.schema_version == "1.1"
    assert validated.positions[0].stock_code == "689001"

    response["schema_version"] = "1.0"
    with pytest.raises(ValidationError):
        EventPortfolioPlanResponse.model_validate(response)

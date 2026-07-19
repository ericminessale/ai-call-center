from types import SimpleNamespace

from app.utils.request_logging import mask_phone, payload_keys, request_summary


def test_request_summary_reports_shape_without_values():
    request = SimpleNamespace(
        method='POST',
        path='/api/webhooks/call-status',
        content_type='application/json',
        content_length=321,
        args={'token': 'do-not-log'},
        form={'From': '+15551234567'},
    )
    payload = {
        'transcript': 'private conversation',
        'Authorization': 'secret',
        'call_id': 'abc-123',
    }

    summary = request_summary(request, payload)

    assert summary['query_keys'] == ['token']
    assert summary['form_keys'] == ['From']
    assert summary['payload_keys'] == ['Authorization', 'call_id', 'transcript']
    rendered = repr(summary)
    assert 'do-not-log' not in rendered
    assert '+15551234567' not in rendered
    assert 'private conversation' not in rendered
    assert 'secret' not in rendered


def test_payload_keys_is_empty_for_non_mapping_payloads():
    assert payload_keys(['one', 'two']) == []
    assert payload_keys(None) == []


def test_mask_phone_retains_only_last_four_digits():
    assert mask_phone('+1 (555) 123-4567') == '***4567'
    assert mask_phone(None) is None
    assert mask_phone('private') == '***'

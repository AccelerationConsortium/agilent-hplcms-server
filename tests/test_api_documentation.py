"""The published reference must describe actual control response envelopes."""
from fastapi.testclient import TestClient
from agilent_hplcms_server.api import create_app


def test_agent_guide_and_error_contract():
    app = create_app()
    client = TestClient(app)
    response = client.get('/docs/agent')
    assert response.status_code == 200
    assert response.json()['protocol_version'] == '1.2'
    schema = client.get('/openapi.json').json()
    assert 'v1.2' in schema['info']['description']
    assert schema == app.openapi()  # repeated reads must not wrap twice
    for path in ['/control/run', '/control/queue']:
        responses = schema['paths'][path]['post']['responses']
        assert 'detail' in responses['409']['content']['application/json']['schema']['properties']
        variants = responses['422']['content']['application/json']['schema']['anyOf']
        assert {v['$ref'].split('/')[-1] for v in variants} == {'LabwareRejection', 'RequestValidationRejection'}
    assert 'detail' in schema['components']['schemas']['LabwareRejection']['properties']

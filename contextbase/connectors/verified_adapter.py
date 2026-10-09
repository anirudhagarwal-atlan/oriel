"""Explicit argument adapters for the pinned verified-source factories.

Importing this module needs only the standard library. Credential objects and
source factories are loaded lazily inside the isolated extraction worker. The
source templates remain unchanged. These mappings have controlled contract
tests; they are not authenticated vendor or actual-dlt extraction evidence.
"""
from __future__ import annotations

import importlib
import json


def _field(name, env, prompt, *, secret=True, required=True):
    return {"name": name, "cli_flag": "--" + name.replace("_", "-"),
            "env_var": env, "prompt": prompt, "secret": secret, "required": required}


# Public, dependency-free discovery metadata. Optional fields have no implicit
# fallback; callers must omit unused references instead of resolving defaults.
ADAPTER_CREDENTIAL_SPECS = {
    "salesforce": [
        _field("username", "SALESFORCE_USERNAME", "Salesforce username", secret=False),
        _field("password", "SALESFORCE_PASSWORD", "Salesforce password"),
        _field("security_token", "SALESFORCE_SECURITY_TOKEN", "Salesforce security token"),
    ],
    "kafka": [
        _field("bootstrap_servers", "KAFKA_BOOTSTRAP_SERVERS", "Kafka bootstrap servers", secret=False),
        _field("group_id", "KAFKA_GROUP_ID", "Explicit Kafka consumer group", secret=False),
        _field("security_protocol", "KAFKA_SECURITY_PROTOCOL", "Kafka security protocol", secret=False),
        _field("sasl_mechanisms", "KAFKA_SASL_MECHANISMS", "Kafka SASL mechanism", secret=False, required=False),
        _field("sasl_username", "KAFKA_SASL_USERNAME", "Kafka SASL username", secret=False, required=False),
        _field("sasl_password", "KAFKA_SASL_PASSWORD", "Kafka SASL password", required=False),
    ],
    "kinesis": [
        _field("stream_name", "KINESIS_STREAM_NAME", "Kinesis stream name", secret=False),
        _field("aws_access_key_id", "AWS_ACCESS_KEY_ID", "AWS access key ID"),
        _field("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "AWS secret access key"),
        _field("region_name", "AWS_REGION", "Explicit AWS region", secret=False),
        _field("aws_session_token", "AWS_SESSION_TOKEN", "Optional AWS session token", required=False),
    ],
    "asana": [_field("access_token", "ASANA_TOKEN", "Asana access token")],
    "google_ads": [
        _field("credentials", "GOOGLE_SERVICE_ACCOUNT_JSON", "Complete service-account JSON value; not a file path"),
        _field("dev_token", "GOOGLE_ADS_DEVELOPER_TOKEN", "Google Ads developer token"),
        _field("impersonated_email", "GOOGLE_ADS_IMPERSONATED_EMAIL", "Delegated Google Ads email", secret=False),
        _field("customer_id", "GOOGLE_ADS_CUSTOMER_ID", "Google Ads customer ID", secret=False),
    ],
    "google_sheets": [
        _field("credentials", "GOOGLE_SERVICE_ACCOUNT_JSON", "Complete service-account JSON value; not a file path"),
    ],
    "google_analytics": [
        _field("property_id", "GA_PROPERTY_ID", "Google Analytics property ID", secret=False),
        _field("credentials", "GOOGLE_SERVICE_ACCOUNT_JSON", "Complete service-account JSON value; not a file path"),
    ],
}

_PARAMETERS = {
    "salesforce": set(), "asana": set(), "google_ads": set(),
    "kafka": {"topics", "batch_size", "batch_timeout", "start_from", "end_time"},
    "kinesis": {"initial_at_timestamp", "max_number_of_messages", "milliseconds_behind_latest",
                "parse_json", "chunk_size"},
    "google_sheets": {"spreadsheet_url_or_id", "range_names", "get_sheets", "get_named_ranges", "max_api_retries"},
    "google_analytics": {"queries", "start_date", "rows_per_page"},
}
SQL_PARAMETER_NAMES = ("schema", "chunk_size", "reflection_level")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _validated(source_type, kwargs):
    if not isinstance(kwargs, dict):
        raise ValueError("Verified-source arguments must be a mapping.")
    fields = ADAPTER_CREDENTIAL_SPECS[source_type]
    allowed = {field["name"] for field in fields} | _PARAMETERS[source_type]
    if set(kwargs) - allowed:
        raise ValueError("Unsupported arguments for this verified-source adapter.")
    for field in fields:
        if (field["required"] or field["name"] in kwargs) and not _text(kwargs.get(field["name"])):
            raise ValueError("Provide every required credential reference and omit unused optional references.")
    return dict(kwargs)


def _explicit_credential(cls, values):
    try:
        # A complete explicit mapping prevents dlt's partial-credential hooks
        # from probing ADC, AWS profiles or other ambient providers.
        result = cls.from_init_value(values)
        if result.is_partial():
            raise ValueError("Incomplete credential mapping")
        return result
    except Exception:
        raise ValueError("The explicit credential mapping is incomplete or invalid.") from None


def _credential(importer, module_name, class_name, values):
    return _explicit_credential(getattr(importer(module_name), class_name), values)


def _kafka_credential(importer, values):
    base = importer("sources.kafka.helpers").KafkaCredentials

    class ReadOnlyKafkaCredentials(base):
        def init_consumer(self):
            configuration = {
                "bootstrap.servers": self.bootstrap_servers, "group.id": self.group_id,
                "security.protocol": self.security_protocol, "auto.offset.reset": "earliest",
                "enable.auto.commit": False, "enable.auto.offset.store": False,
                "enable.ssl.certificate.verification": True,
                "ssl.endpoint.identification.algorithm": "https",
            }
            if self.security_protocol == "SASL_SSL":
                configuration.update({"sasl.mechanisms": self.sasl_mechanisms,
                                      "sasl.username": self.sasl_username,
                                      "sasl.password": self.sasl_password})
            return importer("confluent_kafka").Consumer(configuration)

    return _explicit_credential(ReadOnlyKafkaCredentials, values)


def _provider(provider, importer):
    cls = importer("dlt.common.configuration.providers.dictionary").DictionaryProvider
    if type(provider) is not cls:
        raise ValueError("This adapter requires the worker's explicit private DictionaryProvider.")
    return provider


def _scope_resource_values(source, values, provider):
    # dlt 1.24 resolves resources under (sources, source.section, source.name).
    # Keep these values in the private provider until extraction finishes:
    # already-bound resource objects cannot be rebound with .bind().
    if not _text(getattr(source, "section", None)) or not _text(getattr(source, "name", None)):
        raise ValueError("The verified source has no explicit configuration scope.")
    for key, value in values.items():
        provider.set_value(key, value, None, "sources", source.section, source.name)
    return source


def _service_account(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = item
        return result
    try:
        document = json.loads(value, object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))
        if (not isinstance(document, dict) or document.get("type") != "service_account"
                or any(not _text(document.get(key)) for key in ("project_id", "client_email", "private_key"))):
            raise ValueError("Incomplete service account")
        # dlt's fixed token/auth endpoints are retained; arbitrary JSON keys do
        # not become configuration fields. No credentials file is opened.
        return {key: document[key] for key in ("project_id", "client_email", "private_key", "private_key_id")
                if key in document}
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Supply complete service-account JSON through an environment reference, not a file path.") from None


def _google_ads(values, importer):
    # The pinned get_client helper writes the private key to a temporary file.
    # Compose its unchanged resource functions around an explicit in-memory
    # client instead; SDK construction does not fetch records or refresh tokens.
    info = {**_service_account(values["credentials"]), "type": "service_account",
            "token_uri": "https://oauth2.googleapis.com/token"}
    credential_type = importer("google.oauth2.service_account").Credentials
    native = credential_type.from_service_account_info(info).with_scopes([
        "https://www.googleapis.com/auth/adwords"
    ]).with_subject(values["impersonated_email"])
    client_type = importer("google.ads.googleads.client").GoogleAdsClient
    client = client_type(credentials=native, developer_token=values["dev_token"], use_proto_plus=True)
    module = importer("sources.google_ads")
    dlt = importer("dlt")

    @dlt.source(name="google_ads", section="google_ads")
    def source():
        return [getattr(module, name)(client=client, customer_id=values["customer_id"])
                for name in ("customers", "campaigns", "change_events", "customer_clients")]

    return source()


def _gcp_service_account(value, importer):
    # Both pinned factories accept a union with OAuth first. Pass a complete,
    # resolved service-account instance instead of asking dlt to select a
    # union member from a string or invoke its ADC-capable on_partial hook.
    mapping = _service_account(value)
    auth = _credential(importer, "dlt.sources.credentials", "GcpServiceAccountCredentials", mapping)
    try:
        # from_init_value suppresses resolution errors in dlt 1.24, so checking
        # completeness alone is insufficient before handing it to injection.
        if not auth.is_resolved():
            raise ValueError("Unresolved service account")
    except Exception:
        raise ValueError("The explicit service-account mapping could not be resolved.") from None
    return auth


def build_verified_source(source_type, factory, kwargs, *, resource_config=None,
                          importer=importlib.import_module):
    """Construct a source/resource with explicit credentials and scoped config.

    ``resource_config`` is the DictionaryProvider created by the private worker,
    never a provider discovered from a global container. Tests can inject an
    importer that supplies controlled credential/provider doubles. This does
    not wrap resource return values; the worker owns that separate contract.
    """
    if source_type not in ADAPTER_CREDENTIAL_SPECS:
        return factory(**kwargs)
    values = _validated(source_type, kwargs)
    if source_type == "salesforce":
        auth = _credential(importer, "sources.salesforce.helpers.client", "SecurityTokenAuth", {
            "user_name": values["username"], "password": values["password"],
            "security_token": values["security_token"],
        })
        return factory(credentials=auth)
    if source_type == "kafka":
        topics = values.get("topics")
        if not (_text(topics) or isinstance(topics, list) and topics and all(_text(item) for item in topics)):
            raise ValueError("Kafka requires an explicit nonempty topic or list of topics.")
        protocol = values["security_protocol"]
        if protocol not in {"SSL", "SASL_SSL"}:
            raise ValueError("Kafka requires SSL or SASL_SSL transport.")
        sasl = {"sasl_mechanisms", "sasl_username", "sasl_password"}
        present = sasl & set(values)
        if (protocol.startswith("SASL_") and present != sasl) or (not protocol.startswith("SASL_") and present):
            raise ValueError("Kafka SASL requires all three SASL fields and an explicit SASL security protocol.")
        if protocol == "SASL_SSL" and values["sasl_mechanisms"] not in {"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"}:
            raise ValueError("This adapter supports explicit password-based Kafka SASL mechanisms only.")
        credential_names = {field["name"] for field in ADAPTER_CREDENTIAL_SPECS[source_type]}
        auth = _kafka_credential(importer, {key: values.pop(key) for key in credential_names if key in values})
        return factory(credentials=auth, **values)
    if source_type == "kinesis":
        credential_names = {"aws_access_key_id", "aws_secret_access_key", "region_name", "aws_session_token"}
        auth = _credential(importer, "dlt.common.configuration.specs", "AwsCredentials",
                           {key: values.pop(key) for key in credential_names if key in values})
        return factory(credentials=auth, **values)
    if source_type == "asana":
        provider = _provider(resource_config, importer)
        return _scope_resource_values(factory(), {"access_token": values["access_token"]}, provider)
    if source_type in {"google_sheets", "google_analytics"}:
        auth = _gcp_service_account(values.pop("credentials"), importer)
        return factory(credentials=auth, **values)
    return _google_ads(values, importer)


def sql_parameters(params):
    """Validate the small SQL factory parameter surface before any connection."""
    if params is None:
        return {}
    if not isinstance(params, dict) or set(params) - set(SQL_PARAMETER_NAMES):
        raise ValueError("Unsupported SQL source parameters.")
    if "schema" in params and (not _text(params["schema"]) or "\x00" in params["schema"]):
        raise ValueError("SQL schema must be a nonempty name.")
    if "chunk_size" in params and (type(params["chunk_size"]) is not int or params["chunk_size"] < 1):
        raise ValueError("SQL chunk_size must be a positive integer.")
    if "reflection_level" in params and (not isinstance(params["reflection_level"], str)
                                         or params["reflection_level"] not in {"minimal", "full", "full_with_precision"}):
        raise ValueError("Unsupported SQL reflection level.")
    return dict(params)

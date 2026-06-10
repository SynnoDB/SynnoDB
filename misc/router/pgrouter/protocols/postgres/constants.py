"""Protocol constants and small lookup tables."""

SSL_REQUEST_CODE = 80877103
GSSENC_REQUEST_CODE = 80877104
CANCEL_REQUEST_CODE = 80877102
PROTO_3_0 = 196608
PROTO_3_2 = 196610

FRONTEND_TAGS = {
    b"B": "Bind",
    b"C": "Close",
    b"D": "Describe",
    b"E": "Execute",
    b"H": "Flush",
    b"P": "Parse",
    b"Q": "Query",
    b"S": "Sync",
    b"X": "Terminate",
    b"p": "Password/SASL/GSS response",
    b"f": "CopyFail",
    b"c": "CopyDone",
    b"d": "CopyData",
}

BACKEND_TAGS = {
    b"1": "ParseComplete",
    b"2": "BindComplete",
    b"3": "CloseComplete",
    b"A": "NotificationResponse",
    b"C": "CommandComplete",
    b"D": "DataRow",
    b"E": "ErrorResponse",
    b"G": "CopyInResponse",
    b"H": "CopyOutResponse",
    b"I": "EmptyQueryResponse",
    b"K": "BackendKeyData",
    b"N": "NoticeResponse",
    b"R": "Authentication",
    b"S": "ParameterStatus",
    b"T": "RowDescription",
    b"V": "FunctionCallResponse",
    b"Z": "ReadyForQuery",
    b"c": "CopyDone",
    b"d": "CopyData",
    b"n": "NoData",
    b"s": "PortalSuspended",
    b"t": "ParameterDescription",
    b"v": "NegotiateProtocolVersion",
}

TYPE_OID_NAMES = {
    16: "bool",
    17: "bytea",
    20: "int8",
    21: "int2",
    23: "int4",
    25: "text",
    114: "json",
    700: "float4",
    701: "float8",
    1042: "bpchar",
    1043: "varchar",
    1082: "date",
    1083: "time",
    1114: "timestamp",
    1184: "timestamptz",
    1266: "timetz",
    1700: "numeric",
    2950: "uuid",
    3802: "jsonb",
}


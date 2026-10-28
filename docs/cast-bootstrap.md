# Cast startup auth investigation — 2026-10-22

Static inspection only. No APK/library was installed or executed.

Artifact: Google Cast receiver package `com.google.android.apps.mediashell`,
downloaded from `https://d.apkpure.net/b/APK/com.google.android.apps.mediashell?version=latest`.
APK SHA256: `18af8943077ca76dddf38da0b3c6f74ebfb3f0f3e04c0e58bf8f9637bf739fe8`.
Mirror provenance; APK signing identity has not been independently verified.
ARM64 `libcast_service.so` uses protobuf lite. Addresses below are ELF VAs.
The APK is a different receiver build from the physical Nest's 2025 firmware;
matching type names/endpoint are evidence, not proof of live compatibility.

## GetGoogleOauthTokenResponse

Type name at 0x2419d4, GetTypeName function 0x164acd0.
Constructor 0x164aa44, vtable 0x16cf490.
Parser 0x164aaa8 recognizes field 1, wire type 2, repeated nested messages.
Nested allocation 0x164ae20 creates GoogleOauthCredential objects.

## GoogleOauthCredential

Type name 0x231320, GetTypeName 0x164aa34, parser 0x164a500.
Constructor 0x164a44c, vtable 0x16cf418.

| Field | Wire | Object offset | Use |
| --- | --- | --- | --- |
| 1 | varint | 0x28 | int64; semantics not established, omitted locally |
| 2 | bytes | 0x18 | token string, copied to AuthToken+0x20 |
| 3 | varint | 0x30 | int64; semantics not established, omitted locally |
| 4 | varint | 0x38 | auth error enum; absent/0 maps to success |
| 5 | bytes | 0x20 | account ID string, copied to AuthToken+0x00 |

Consumer: RobotAccountAuthManager source path string at 0x267227,
`../chromecast/internal/user_auth/robot_account_auth/robot_account_auth_manager.cc`.
HTTP status 200 branch 0xc6ad80 constructs response at 0xc6b168,
parses via 0xf97078, iterates credentials and calls 0xc6b174.
Credential consumer 0xc6b174 reads field 4 enum; success requires both strings
nonempty, otherwise logs `AuthError mismatch. GaiaId/token is empty.`.
At 0xc6b204 field 2 is copied to stack+0x20, then AuthToken+0x20.
At 0xc6b344 field 5 is copied to stack+8, then AuthToken+0.
Cached credential lookup 0xc6a134 compares AuthToken+0 to the requested account
identifier. Fields 1/3 are not read by this consumer.

The physical Nest's captured request uses a newer request schema than this APK
(field 4 is bytes, while APK expects enum), so request layout is not reused.
Its response layout still needs physical-device verification.

## Local implementation

`app/bootstrap.py` reads three linked account IDs from an existing owned-device
S3 capture at field path 116159442/3/4/1/2/2/3/1. The private ID list is stored
in `data/offline-account-ids.json`, mode 0600. No captured Google token is reused.
Reply: repeated field 1 credentials, with field 2 a fixed local-only string,
field 4 zero, field 5 each captured ID. No expiry field semantics are guessed.
Missing/invalid local ID data fails closed with local HTTP 503.
All Google forwarding remains disabled.

This replaces the incorrect earlier 34-byte flat field1-string/field2-time probe.
Tests validate encoding, ID extraction, routing and fail-closed behavior, not
physical Nest acceptance.

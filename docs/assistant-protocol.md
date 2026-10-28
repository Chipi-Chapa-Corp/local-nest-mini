# Native S3 endpointer analysis

Source: Google-published google-assistant-library 1.1.0 x86_64 wheel
https://pypi.org/project/google-assistant-library/1.1.0/
Wheel SHA256: 30d864f2ded52024c34d88afcfe9c1070f7e3f980d6634e86fecc6bf7fb3b079
Downloaded and inspected statically. The library was not loaded or executed.

- S3Session response handler at 0x75f29c loads extension b3e068.
- Its initializer 0x9f758e writes extension number 0x1320f9 = 1253625.
- Recognition handler checks presence bit8, loads child at offset0x30,
  then calls ProcessEndpointerEvent at 0x75fbe0.
- At 0x75fc6d the handler reads event enum at object offset0x28.
- Enum0 goes to onSpeechStart. Enum1 and enum3 go to the same onSpeechEnd
  branch (0x75fd5f), whose log text is at 0xa9b58. Enum2 goes to the
  Unknown endpointer event branch (0x75fe77).
- greco.EndpointerEvent has vtable b22268; GetTypeName at 0x9ebd10.
- Parser 0x9eb610 reads wire tag8 (field1) and writes enum to offset0x28
  at 0x9eb881. Field2 is int64 at offset0x18; field3 at offset0x20.
- Captured Nest response frame101 has 1253625.child2.field1=3, followed
  by frame102 enum1, frame103 enum2. The previous test sent enum2 only
  and failed: live flow f27a586d20484379a8b8319ed3d920b8 continued to60s.

The old client library is evidence for event semantics, not proof of current
Nest behavior. A live owned-device test of enum3 remains required.
No forwarding to Google is needed or permitted for this test.

Follow-up: the real Nest ignored the minimal type3 event too (flow
256daa5a80cb4a4393f2401081a90b4d). This rules out claiming the enum fix
alone solves current-device endpointing.

Extension124211619 (0x76751a3) is DeviceSelectionDecision, not a generic
stop-recording message: initializer0x9db81a -> default b3d2e0 -> vtable
b20dc8 -> GetTypeName0x9db430 returns speech.s3.DeviceSelectionDecision.
S3Session handler0x760030 logs that name and passes the decision onward.
Its captured child1=1 is the arbitration decision. Do not describe it as
end-of-speech without further evidence.

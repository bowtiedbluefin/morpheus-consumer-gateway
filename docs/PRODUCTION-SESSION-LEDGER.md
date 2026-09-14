# Funded session ledger

Every listed session is closed. Amounts are MOR collateral, not MOR expenditure. Final withdrawal accounting is in the production-readiness report.

| Session ID | Model | Stake (MOR) | Closed at (UTC) |
| --- | --- | ---: | --- |
| `0xe3b1d0a5476c321bbe5605f3cd7e90c0759d96c3b30f9de2eea77dc7fe2a821c` | DeepSeek v4 Flash | 2.678757416389313151 | 2026-09-13 22:39:07 |
| `0x4794b1871a23c0a569aaf00dca70f8c2c05c22c6aa4fa4be78305f467e8c4062` | DeepSeek v4 Flash | 0.892919467156707374 | 2026-09-13 22:42:01 |
| `0x0aa6af5d35e383c3d5bfd3043d5aaf3f482d7d88dbcd03623af261585cf363e5` | DeepSeek v4 Flash | 0.892919476668612488 | 2026-09-13 22:44:01 |
| `0x603635deac2bb444f12ed02bd45c05d57be8d329722b8c3005e780791199b0c7` | DeepSeek v4 Flash | 0.892919476668612488 | 2026-09-13 22:39:01 |
| `0x66369ef8cf03a5b46ac32329f014e3b38a9e3b0e760093be9aedf95e23423a34` | DeepSeek v4 Flash | 1.339379434274786557 | 2026-09-13 22:58:01 |
| `0xd18ce5aa1b78fde8d48b49edd1211960a850d0a6d3df1c117a19efe5299c0606` | Llama 3.1 8B | 0.569645800481160986 | 2026-09-13 22:54:45 |
| `0x75df39daf89fd2426b56a6ca576dc4a4aa7e9008c17edcce5334137aed4fe5a2` | DeepSeek v4 Flash | 1.138717546147394126 | 2026-09-13 23:02:27 |
| `0x7e6251eb219fcf336eae94bd0b6a287db7ca52b538f1ca7ef4c7c4a865230567` | DeepSeek v4 Flash | 0.447948188781966101 | 2026-09-13 23:12:15 |
| `0x11f3fd6d8ef24334ca586c0c4c72a9c75c8f8c9cd5287343959bbd18fe3ac37b` | DeepSeek v4 Flash | 0.447948193553776055 | 2026-09-13 23:15:17 |
| `0xe7b25f8e3067b76d9f1347bf4df626755654389606d4601875c916ffe3110590` | DeepSeek v4 Flash | 0.447948266207947205 | 2026-09-13 23:18:19 |
| `0x9ae7e15cf2c4626ebc1aee9ec64710e1f5508c14a01ee4a70b45bf0a16508e24` | DeepSeek v4 Flash | 0.447948266207947205 | 2026-09-13 23:19:21 |

The original 30-minute session is included. Ten additional funded sessions were created during this campaign. Nineteen failed gateway rows have no on-chain session ID. Two failed five-minute attempts nevertheless mined approval transactions; these are included in the transaction ledger and ETH fee accounting.

Automatic withdrawal subsequently returned 7.392395593543746086 MOR. The wallet ended with **20.5 liquid MOR, zero held MOR and zero withdrawable MOR**. Withdrawal transaction: `0x84ae5ecc5cb6a6d9be9ad8ef6aaeeede620649550b00b5f7113f140f9eceb47d`. All 35 recorded transactions have successful receipts; exact fee reconciliation is in the [evidence JSON](PRODUCTION-TEST-EVIDENCE.json).

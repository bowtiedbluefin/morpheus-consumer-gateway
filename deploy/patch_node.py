"""Apply a minimal, fail-closed patch to the exact pinned node source tree."""

import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])


def replace(file, old, new, count=1):
    p = root / file
    text = p.read_text()
    if text.count(old) != count:
        raise SystemExit(f"Pinned source mismatch: {file}: expected {count} occurrences")
    p.write_text(text.replace(old, new))


for name in ("gateway_progress.go", "gateway_progress_test.go"):
    shutil.copy(Path(__file__).parent / "native" / name, root / "internal/lib" / name)
replace(
    "internal/blockchainapi/structs/req.go",
    "type OpenSessionWithDurationRequest struct {",
    'type OpenSessionWithDurationRequest struct {\n MaxStakeWei *lib.BigInt `json:"maxStakeWei"`',
)
replace(
    "internal/system/structs.go",
    "type ConfigResponse struct {",
    "type ConfigResponse struct {\n GatewayCapabilities []string",
)
replace(
    "internal/system/controller.go",
    "Version: config.BuildVersion,",
    'Version: config.BuildVersion,\n GatewayCapabilities: []string{"stake-limit-v1", "operation-journal-v1", "transaction-progress-v1"},',
)
controller = root / "internal/blockchainapi/controller.go"
s = controller.read_text()
for name in ("openSessionByBid", "closeSession", "withdrawUserStakes"):
    signature = (
        (
            "func (c *BlockchainController) "
            if name != "openSessionByBid"
            else "func (s *BlockchainController) "
        )
        + name
        + "(ctx *gin.Context) {"
    )
    assert s.count(signature) == 1
    injection = """
 progress, progressErr := lib.BeginGatewayOperation(ctx.GetHeader("X-Gateway-Operation"))
 if progressErr != nil {ctx.JSON(http.StatusConflict, gin.H{"error":"operation exists or journal unavailable"});return}
 ctx.Set(lib.GatewayProgressKey,progress)
 defer func(){progress.Finish(ctx.Writer.Status())}()
"""
    s = s.replace(signature, signature + injection)
    start = s.index(signature)
    end = s.index("\n}\n", start) + 3
    part = s[start:end].replace(
        "structs.ErrRes{Error: err.Error()}",
        'gin.H{"error":err.Error(), "progress":progress, "sessionID":progress.SessionID}',
    )
    if name == "openSessionByBid":
        part = part.replace(
            "sessionId, err :=",
            """if reqPayload.MaxStakeWei != nil {
 ctx.Set(lib.GatewayMaxStakeKey,reqPayload.MaxStakeWei.Unpack())
 }
 sessionId, err :=""",
        )
        part = part.replace(
            "structs.OpenSessionRes{SessionID: sessionId}", 'gin.H{"sessionID":sessionId,"progress":progress}'
        )
    else:
        part = part.replace("structs.TxRes{Tx: txHash}", 'gin.H{"tx":txHash,"progress":progress}')
    s = s[:start] + part + s[end:]
controller.write_text(s)
replace(
    "internal/blockchainapi/service.go",
    '\tlog.Infof("attempting to initiate session %s",',
    '\tif err := lib.CheckGatewayStake(ctx, amountTransferred); err != nil {return common.Hash{}, false, err}\n\tlog.Infof("attempting to initiate session %s",',
)
replace(
    "internal/blockchainapi/service.go",
    "\t// Poll until the session is visible on-chain",
    "\tlib.GatewaySession(ctx,sessionID)\n\t// Poll until the session is visible on-chain",
)
for kind, old in (
    ("approval", "return s.morToken.IncreaseAllowanceTx(opts, s.diamonContractAddr, stake)"),
    ("open", "return s.sessionRouter.OpenSessionTx(opts, approval, approvalSig, stake, directPayment)"),
    ("close", "return s.sessionRouter.CloseSessionTx(opts, reportMessage, signedReport)"),
):
    new = (
        f'if err := lib.GatewayAttempt(ctx,"{kind}"); err != nil {{return nil,err}}\n tx,err := '
        + old.removeprefix("return ")
        + f'\n if opts.NoSend && err != nil && tx == nil {{lib.GatewayBuildFailure(ctx,"{kind}")}}\n lib.GatewayRecord(ctx,"{kind}",tx)\n return tx,err'
    )
    replace("internal/blockchainapi/service.go", old, new)
replace(
    "internal/repositories/registries/session_router.go",
    "tx, err := g.sessionRouter.WithdrawUserStakes(opts, user, iterations)",
    'if err := lib.GatewayAttempt(opts.Context,"withdraw");err!=nil{return common.Hash{},err}\n tx, err := g.sessionRouter.WithdrawUserStakes(opts, user, iterations)\n lib.GatewayRecord(opts.Context,"withdraw",tx)',
)
# Bundled mode has one durable owner of expiry cleanup; retain cache rehydration.
replace(
    "internal/blockchainapi/session_expiry_handler.go",
    "\ts.rehydrateFromChain(ctx)",
    "\ts.rehydrateFromChain(ctx)\n if lib.GatewayManaged() { <-ctx.Done(); return ctx.Err() }",
)
# Hold across approval+open, including disconnected HTTP clients.
for signature in (
    "func (s *BlockchainService) OpenSession(ctx context.Context, approval, approvalSig []byte, stake *big.Int, directPayment bool, agentUsername string, isTee bool) (common.Hash, error) {",
    "func (s *BlockchainService) CloseSession(ctx context.Context, sessionID common.Hash) (common.Hash, error) {",
    "func (s *BlockchainService) WithdrawUserStakes(ctx context.Context, iterations uint8) (common.Hash, error) {",
):
    replace(
        "internal/blockchainapi/service.go",
        signature,
        signature
        + "\n unlock, lockErr := lib.GatewayWalletLock(ctx); if lockErr != nil {return common.Hash{},lockErr}; defer unlock()",
    )

package lib
import("context";"math/big";"os";"path/filepath";"testing";"encoding/json")
func TestGatewayStakeGuard(t *testing.T){
 ctx:=context.WithValue(context.Background(),GatewayMaxStakeKey,big.NewInt(5))
 if CheckGatewayStake(ctx,big.NewInt(6))==nil{t.Fatal("over-limit stake accepted")}
 if CheckGatewayStake(ctx,big.NewInt(5))!=nil{t.Fatal("exact limit rejected")}
}
func TestGatewayJournalPersistsAndRejectsDuplicate(t *testing.T){
 root:=t.TempDir();t.Setenv("GATEWAY_JOURNAL_PATH",root);id:="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
 p,err:=BeginGatewayOperation(id);if err!=nil{t.Fatal(err)}
 ctx:=context.WithValue(context.Background(),GatewayProgressKey,p)
 if err:=GatewayAttempt(ctx,"open");err!=nil{t.Fatal(err)};p.Finish(500)
 data,err:=os.ReadFile(filepath.Join(root,id+".json"));if err!=nil{t.Fatal(err)}
 var got GatewayProgress;if err=json.Unmarshal(data,&got);err!=nil{t.Fatal(err)}
 if !got.Completed || got.Stage!="submitting_open" || got.HTTPStatus!=500{t.Fatal(string(data))}
 if _,err:=BeginGatewayOperation(id);err==nil{t.Fatal("duplicate operation accepted")}
}
func TestGatewayWalletLockCancellation(t *testing.T){
 t.Setenv("GATEWAY_JOURNAL_PATH",t.TempDir())
 unlock,err:=GatewayWalletLock(context.Background());if err!=nil{t.Fatal(err)};defer unlock()
 ctx,cancel:=context.WithCancel(context.Background());cancel()
 if _,err:=GatewayWalletLock(ctx);err==nil{t.Fatal("concurrent wallet mutation allowed")}
}
func TestGatewayJournalFailsClosedWhenDiskUnavailable(t *testing.T){
 root:=t.TempDir();t.Setenv("GATEWAY_JOURNAL_PATH",root)
 p,err:=BeginGatewayOperation("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");if err!=nil{t.Fatal(err)}
 if err:=os.RemoveAll(root);err!=nil{t.Fatal(err)}
 ctx:=context.WithValue(context.Background(),GatewayProgressKey,p)
 if GatewayAttempt(ctx,"open")==nil{t.Fatal("submission authorized without durable intent")}
}
func TestGatewayBuildFailureOnlyReleasesUnsubmittedAttempt(t *testing.T){
 p:=&GatewayProgress{Stage:"submitting_open"}
 ctx:=context.WithValue(context.Background(),GatewayProgressKey,p)
 GatewayBuildFailure(ctx,"open");if p.Stage!="not_submitted"{t.Fatal("proven pre-broadcast failure blocked")}
 p.Stage="submitting_open";p.Transactions=append(p.Transactions,GatewayTransaction{Kind:"open"})
 GatewayBuildFailure(ctx,"open");if p.Stage=="not_submitted"{t.Fatal("previous unknown transaction forgotten")}
}

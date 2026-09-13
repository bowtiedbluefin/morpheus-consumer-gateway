// Gateway additions, Copyright 2026 bowtiedbluefin. MIT licensed.
package lib

import (
    "context"
    "encoding/json"
    "fmt"
    "math/big"
    "os"
    "path/filepath"
    "regexp"
    "time"

    "github.com/ethereum/go-ethereum/common"
    "github.com/ethereum/go-ethereum/core/types"
)

const GatewayProgressKey = "morpheus_gateway_progress_v1"
const GatewayMaxStakeKey = "morpheus_gateway_max_stake_v1"

type GatewayTransaction struct { Kind string `json:"kind"`; Hash common.Hash `json:"hash"` }
type GatewayProgress struct {
    Stage string `json:"stage"`
    Completed bool `json:"completed"`
    HTTPStatus int `json:"http_status"`
    SessionID common.Hash `json:"sessionID"`
    Transactions []GatewayTransaction `json:"transactions"`
    UpdatedAt int64 `json:"updated_at"`
    path string
}

func BeginGatewayOperation(id string) (*GatewayProgress, error) {
    p := &GatewayProgress{Stage:"not_submitted", Transactions:[]GatewayTransaction{}}
    if id == "" { return p, nil }
    if !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(id) { return nil, fmt.Errorf("invalid gateway operation ID") }
    root := os.Getenv("GATEWAY_JOURNAL_PATH")
    if root == "" { return nil, fmt.Errorf("gateway operation journal is not configured") }
    if err := os.MkdirAll(root, 0700); err != nil { return nil, err }
    p.path = filepath.Join(root,id+".json")
    // Never execute the same operation ID twice, including after a restart.
    file,err:=os.OpenFile(p.path,os.O_WRONLY|os.O_CREATE|os.O_EXCL,0600)
    if err != nil { return nil, fmt.Errorf("gateway operation already exists or journal unavailable") }
    file.Close()
    if err:=p.persist();err!=nil{return nil,err}
    return p,nil
}
func (p *GatewayProgress) persist() error {
    if p==nil || p.path=="" {return nil}
    p.UpdatedAt=time.Now().Unix()
    data,err:=json.Marshal(p);if err!=nil{return err}
    temp,err:=os.OpenFile(p.path+".tmp",os.O_CREATE|os.O_TRUNC|os.O_WRONLY,0600);if err!=nil{return err}
    if _,err=temp.Write(data);err!=nil{temp.Close();return err}
    if err=temp.Sync();err!=nil{temp.Close();return err};if err=temp.Close();err!=nil{return err}
    if err=os.Rename(p.path+".tmp",p.path);err!=nil{return err}
    dir,err:=os.Open(filepath.Dir(p.path));if err!=nil{return err};defer dir.Close();return dir.Sync()
}
func GatewayProgressFor(ctx context.Context) *GatewayProgress {p,_:=ctx.Value(GatewayProgressKey).(*GatewayProgress);return p}
func GatewayAttempt(ctx context.Context,kind string) error {
    if p:=GatewayProgressFor(ctx);p!=nil {previous:=p.Stage;p.Stage="submitting_"+kind;if err:=p.persist();err!=nil{p.Stage=previous;return err};return nil};return nil
}
func GatewayRecord(ctx context.Context,kind string,tx *types.Transaction) {
    if p:=GatewayProgressFor(ctx);p!=nil && tx!=nil {p.Transactions=append(p.Transactions,GatewayTransaction{kind,tx.Hash()});_ = p.persist()}
}
func GatewaySession(ctx context.Context,id common.Hash) {
    if p:=GatewayProgressFor(ctx);p!=nil {p.SessionID=id;_ = p.persist()}
}
func (p *GatewayProgress) Finish(status int) {p.Completed=true;p.HTTPStatus=status;_ = p.persist()}
func CheckGatewayStake(ctx context.Context,amount *big.Int) error {
    limit,_:=ctx.Value(GatewayMaxStakeKey).(*big.Int)
    if limit!=nil && (limit.Sign()<=0 || amount.Cmp(limit)>0) {return fmt.Errorf("gateway stake limit exceeded")};return nil
}

var gatewayWallet = make(chan struct{},1)
func GatewayManaged() bool {return os.Getenv("GATEWAY_JOURNAL_PATH")!=""}
func GatewayWalletLock(ctx context.Context) (func(),error) {
 if !GatewayManaged() {return func(){},nil}
 select {case gatewayWallet<-struct{}{}: return func(){<-gatewayWallet},nil;case <-ctx.Done():return nil,ctx.Err()}
}

// A failed NoSend builder cannot have broadcast this attempt. Previous hashes
// for the same mutation still make its overall outcome uncertain. Approval
// hashes may precede an open builder; reaching it means approval was mined.
func GatewayBuildFailure(ctx context.Context,kind string) {
 p:=GatewayProgressFor(ctx);if p==nil{return}
 for _,tx:=range p.Transactions {if tx.Kind==kind {return}}
 p.Stage="not_submitted";_ = p.persist()
}

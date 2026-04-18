package main

import (
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"

	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"google.golang.org/grpc"

	"amaze/go_processor/internal/api"
	"amaze/go_processor/internal/processor"
	"amaze/go_processor/internal/stats"
	"amaze/go_processor/internal/store"
)

func main() {
	port := os.Getenv("POLICY_PROCESSOR_PORT")
	if port == "" {
		port = "50051"
	}
	statsAddr := os.Getenv("STATS_ADDR")
	if statsAddr == "" {
		statsAddr = ":8081"
	}
	policyPath := os.Getenv("POLICY_PATH")

	if err := store.Init(policyPath); err != nil {
		fmt.Fprintf(os.Stderr, "failed to load policies: %v\n", err)
		os.Exit(1)
	}

	col := stats.NewCollector()
	if col == nil {
		fmt.Fprintln(os.Stderr, "stats.NewCollector returned nil")
		os.Exit(1)
	}
	api.StartHTTP(statsAddr, col)
	fmt.Printf("[policy-processor] stats API on %s\n", statsAddr)

	lis, err := net.Listen("tcp", ":"+port)
	if err != nil {
		fmt.Fprintf(os.Stderr, "listen error: %v\n", err)
		os.Exit(1)
	}

	srv := grpc.NewServer(
		grpc.MaxRecvMsgSize(10*1024*1024),
		grpc.MaxSendMsgSize(10*1024*1024),
	)
	extprocv3.RegisterExternalProcessorServer(srv, &processor.Server{Stats: col})

	done := make(chan struct{})
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGHUP, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		for sig := range sigCh {
			switch sig {
			case syscall.SIGHUP:
				if err := store.Get().Reload(); err != nil {
					fmt.Fprintf(os.Stderr, "[policy-processor] reload error: %v\n", err)
				} else {
					fmt.Println("[policy-processor] policies reloaded")
				}
			default:
				fmt.Println("[policy-processor] shutting down")
				srv.GracefulStop()
				close(done)
				return
			}
		}
	}()

	fmt.Printf("[policy-processor] listening on :%s\n", port)
	srv.Serve(lis)
	<-done
}

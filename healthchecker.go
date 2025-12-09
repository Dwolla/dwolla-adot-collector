package main

import (
	"fmt"
	"net/http"
	"os"
	"time"
)

func main() {
	// The port the OpenTelemetry Collector's health_check extension is listening on
	const healthCheckURL = "http://localhost:13133"

	client := http.Client{
		Timeout: 5 * time.Second, // Set a timeout
	}

	resp, err := client.Get(healthCheckURL)
	if err != nil {
		fmt.Printf("Health check failed: %v\n", err)
		os.Exit(1)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		fmt.Printf("Health check failed with status code: %d\n", resp.StatusCode)
		os.Exit(1)
	}

	fmt.Println("Health check successful")
	os.Exit(0) // Success
}

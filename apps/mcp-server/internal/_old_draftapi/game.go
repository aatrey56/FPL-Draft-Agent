package draftapi

// Game represents the response from /api/game
// We only model the fields we need.
type Game struct {
	CurrentEvent int  `json:"current_event"`
	NextEvent    int  `json:"next_event"`

	CurrentEventFinished bool `json:"current_event_finished"`

	ProcessingStatus string `json:"processing_status"`
	WaiversProcessed bool   `json:"waivers_processed"`
}
package db

import "fmt"

// DB is the database client that wraps a connection.
type DB struct {
	ConnectionString string
	connected        bool
}

// NewDB creates a new database client with the given connection string.
func NewDB(connectionString string) *DB {
	return &DB{
		ConnectionString: connectionString,
		connected:        false,
	}
}

// Connect establishes a connection to the database.
func (d *DB) Connect() error {
	if d.connected {
		return fmt.Errorf("already connected")
	}
	d.connected = true
	return nil
}

// Close closes the database connection.
func (d *DB) Close() error {
	if !d.connected {
		return fmt.Errorf("not connected")
	}
	d.connected = false
	return nil
}

// Query executes a query string and returns the raw result rows.
func (d *DB) Query(query string, args ...interface{}) ([]map[string]interface{}, error) {
	if !d.connected {
		return nil, fmt.Errorf("database not connected")
	}
	// Stub: return empty result set
	return []map[string]interface{}{}, nil
}

// Exec executes a statement that does not return rows.
func (d *DB) Exec(query string, args ...interface{}) error {
	if !d.connected {
		return fmt.Errorf("database not connected")
	}
	return nil
}

# Unit Tests Guide for Dialer Utils

## Running Tests

### Run all tests
```bash
pytest
# or with Docker
docker-compose exec django pytest
```

### Run specific test class
```bash
pytest dialer/tests.py::TestAddToPriorityQueue
docker-compose exec django pytest dialer/tests.py::TestAddToPriorityQueue
```

### Run specific test
```bash
pytest dialer/tests.py::TestAddToPriorityQueue::test_add_entry_success
```

### Run tests with markers
```bash
# Run only lock tests
pytest -m lock

# Run only Redis tests
pytest -m redis

# Run only data integrity tests
pytest -m integrity
```

### Run with coverage report
```bash
pytest --cov=dialer.utils --cov-report=html
# Open htmlcov/index.html to view coverage
```

### Run in verbose mode
```bash
pytest -v
```

### Run and stop at first failure
```bash
pytest -x
```

### Run with output capture disabled (see print statements)
```bash
pytest -s
```

## Test Coverage

### What's Tested

1. **Lock Integrity** (TestLockIntegrity)
   - Lock acquisition succeeds/fails
   - Lock is released on success
   - Lock is released on exception
   - Lock timeout configuration

2. **Queue Operations** (TestAddToPriorityQueue)
   - Adding entries with lock protection
   - Lock timeout handling
   - Exception handling
   - Queue data append integrity

3. **Agent Queue Operations** (TestAgentQueueOperations)
   - Adding agents to sales queue
   - Adding agents to support queue
   - Retrieving idle agents (all types)
   - Queue ordering

4. **Active Calls** (TestActiveCallsOperations)
   - Getting active calls (sales/support/all)
   - Removing calls with pipeline
   - Non-existent call handling
   - Pipeline atomicity

5. **Agent State** (TestAgentStateOperations)
   - Getting individual agent state
   - Getting all agent states
   - Agent team retrieval

6. **Next Available Agent** (TestGetNextAvailableAgent)
   - Successful agent retrieval
   - Empty queue handling
   - Exception handling

7. **Redis Data Integrity** (TestRedisDataIntegrity)
   - Queue data validity after operations
   - Pipeline atomicity verification
   - JSON encoding/decoding consistency

8. **Edge Cases** (TestEdgeCases)
   - Empty entries
   - Large entries
   - Multiple concurrent operations

## Test Statistics

- Total Tests: 35+
- Coverage Target: 80%
- Mock Usage: Full Redis isolation (no real Redis required)

## Key Testing Patterns

### 1. Lock Testing
Tests verify that:
- Lock is acquired before operation
- Lock is released after operation (success or error)
- Timeout is properly configured

```python
def test_lock_is_released_on_success(self, mock_conn):
    mock_lock = MagicMock()
    mock_lock.acquire.return_value = True
    mock_lock.owned.return_value = True
    
    add_to_priority_queue({"phone_number": "123"})
    
    mock_lock.release.assert_called_once()  # Verified
```

### 2. Redis Integrity Testing
Tests verify:
- Data structure validity (JSON encoding/decoding)
- Pipeline operations are atomic
- Original data isn't corrupted

```python
def test_queue_data_remains_valid_after_add(self, mock_conn):
    # Verify data integrity through encoding/decoding
    saved_data = json.loads(mock_conn.set.call_args[0][1])
    assert len(saved_data) == 2  # Correct length
    assert saved_data[0] == existing_queue[0]  # Data preserved
```

### 3. Exception Handling Testing
Tests verify:
- Exceptions are caught and logged
- Operations fail gracefully
- Locks are released even on error

```python
def test_add_entry_exception_handling(self, mock_conn, mock_logger):
    mock_lock.acquire.side_effect = Exception("Redis error")
    add_to_priority_queue({"phone_number": "123"})
    
    mock_logger.error.assert_called()  # Error logged
    mock_lock.release.assert_called_once()  # Lock released
```

## Mocking Strategy

All Redis operations are mocked to:
- Avoid dependencies on actual Redis
- Allow testing failure scenarios
- Enable fast test execution
- Isolate unit tests from infrastructure

### Mock Hierarchy
```
mock_conn                    # Redis connection
├── conn.get()              # Mocked for priority queue
├── conn.set()              # Mocked for saving data
├── conn.zadd()             # Mocked for sorted sets
├── conn.zpopmin()          # Mocked for FIFO retrieval
├── conn.hgetall()          # Mocked for hash operations
├── conn.lock()             # Returns mock_lock
│   └── mock_lock
│       ├── acquire()       # Can succeed/fail
│       ├── release()       # Can succeed/fail
│       └── owned()         # Can return True/False
└── conn.pipeline()         # Returns mock_pipe
    └── mock_pipe
        ├── hget()          # Queued operation
        ├── hdel()          # Queued operation
        └── execute()       # Executes all queued
```

## Running Tests in Docker

```bash
# Build and run tests
docker-compose exec django pytest -v

# Run with coverage
docker-compose exec django pytest --cov=dialer.utils --cov-report=term-missing

# Run specific test class
docker-compose exec django pytest dialer/tests.py::TestLockIntegrity -v

# Interactive test debugging
docker-compose exec django pytest -s dialer/tests.py::TestAddToPriorityQueue::test_add_entry_success
```

## Continuous Integration

### Pre-commit
```bash
pytest dialer/tests.py --cov=dialer.utils --cov-fail-under=80
```

### CI Pipeline Example
```yaml
test:
  script:
    - pytest dialer/tests.py --cov=dialer.utils --cov-report=xml
    - coverage report
```

## Troubleshooting

### "No module named 'dialer'" Error
```bash
cd /home/abdulrauf/Desktop/voice_orchestrator/voice_orchestrator
export PYTHONPATH=$PWD:$PYTHONPATH
pytest
```

### Mock not working as expected
- Verify patch path is correct: `patch('dialer.utils.conn')`
- Check that imports in tests.py match the actual module
- Ensure fixture scope is appropriate

### Tests timeout
- Check for infinite loops in test setup
- Verify mock returns are not None when expecting data
- Increase pytest timeout: `pytest --timeout=300`

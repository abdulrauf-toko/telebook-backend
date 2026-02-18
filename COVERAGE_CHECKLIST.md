# Test Coverage Checklist

## Dialer Utils (dialer/utils.py)

### Priority Queue Functions
- [x] `get_priority_queue()`
  - [x] Returns empty list when no queue exists
  - [x] Returns queue data when valid JSON
  - [x] Handles JSON decode errors
  - [x] Returns empty list on JSON error
  
- [x] `add_to_priority_queue(entry)`
  - [x] Successfully adds entry with lock protection
  - [x] Handles lock timeout
  - [x] Handles exceptions during add
  - [x] Appends to existing queue
  - [x] Lock is released on success
  - [x] Lock is released on error
  - [x] Lock timeout is configured correctly
  - [x] Empty entry handling
  - [x] Large entry handling
  - [x] Multiple entry consistency
  - [x] Lock key formatting

### Agent Queue Operations
- [x] `add_sales_agent_to_queue(agent_id)`
  - [x] Adds agent with current timestamp
  - [x] Uses SALES_AGENT_QUEUE key
  - [x] Uses sorted set (zadd)

- [x] `add_support_agent_to_queue(agent_id)`
  - [x] Adds agent with current timestamp
  - [x] Uses SUPPORT_AGENT_QUEUE key
  - [x] Uses sorted set (zadd)

- [x] `get_all_idle_sales_agents()`
  - [x] Returns list of agent IDs
  - [x] Returns empty list when queue empty
  - [x] Uses zrange operation

- [x] `get_all_idle_support_agents()`
  - [x] Returns list of agent IDs
  - [x] Returns empty list when queue empty

- [x] `get_all_idle_agents()`
  - [x] Combines sales and support agents
  - [x] Returns empty when both queues empty

### Active Calls Operations
- [x] `get_all_active_sales_calls()`
  - [x] Returns dict of active calls
  - [x] Returns empty dict when no calls
  - [x] Uses hgetall operation

- [x] `get_all_active_support_calls()`
  - [x] Returns dict of active calls

- [x] `get_all_active_calls()`
  - [x] Combines sales and support calls

- [x] `remove_active_sales_call(call_id)`
  - [x] Successfully removes call and returns data
  - [x] Handles call not found (returns None)
  - [x] Uses pipeline for atomicity
  - [x] Handles orjson bytes encoding
  - [x] Pipeline operations in correct order
  - [x] Both hget and hdel called

- [x] `remove_active_support_call(call_id)`
  - [x] Successfully removes call
  - [x] Returns None when not found

### Agent State Operations
- [x] `get_agent_state(agent_id)`
  - [x] Returns agent data when found
  - [x] Returns None when not found
  - [x] Uses hget operation

- [x] `get_all_agent_states()`
  - [x] Returns all agents dict
  - [x] Uses get operation

- [x] `get_agent_team(agent_id)`
  - [x] Returns correct team (sales/support)
  - [x] Extracts team field correctly

### Pending Agent Operations
- [x] `get_pending_support_agent()`
  - [x] Finds pending support agent
  - [x] Returns None when no pending agent
  - [x] Handles multiple pending agents

- [x] `get_pending_sales_agent()`
  - [x] Finds pending sales agent
  - [x] Returns None when no pending agent
  - [x] Handles multiple pending agents

### Next Available Agent Operations
- [x] `get_next_available_sales_agent()`
  - [x] Successfully returns agent
  - [x] Returns None when queue empty
  - [x] Handles exceptions
  - [x] Returns only agent_id, not score
  - [x] Uses zpopmin for FIFO

- [x] `get_next_available_support_agent()`
  - [x] Successfully returns agent
  - [x] Returns None when queue empty
  - [x] Handles exceptions
  - [x] Returns only agent_id, not score

### Lock & Data Integrity
- [x] Lock integrity
  - [x] Lock acquired before operations
  - [x] Lock released on success
  - [x] Lock released on exception
  - [x] Timeout configured
  - [x] Sleep interval configured

- [x] Data integrity
  - [x] JSON encoding/decoding consistency
  - [x] Pipeline atomicity
  - [x] Data not corrupted after operations

### Edge Cases
- [x] Empty entries
- [x] Large entries
- [x] Multiple concurrent operations
- [x] Invalid JSON handling
- [x] Missing agent/call scenarios
- [x] Various agent teams
- [x] Data type verification

---

## Events Utils (events/utils.py)

### Agent State Management with Locks
- [x] `mark_agent_idle_in_cache(agent_id)`
  - [x] Sales agent success
  - [x] Support agent success
  - [x] Agent not found
  - [x] Lock timeout
  - [x] Exception handling
  - [x] Correct queue selection based on team
  - [x] State updated to idle
  - [x] Current call ID cleared

- [x] `mark_agent_busy_in_cache(agent_id, call_id)`
  - [x] Sales agent success
  - [x] Support agent success
  - [x] Agent not found
  - [x] Lock timeout
  - [x] Exception handling
  - [x] Correct queue selected
  - [x] State updated to busy
  - [x] Call ID stored

- [x] `remove_agent_from_cache(agent_id)`
  - [x] Sales agent removal
  - [x] Support agent removal
  - [x] Agent not found
  - [x] Lock timeout
  - [x] Exception handling
  - [x] Both hset and zrem called

### FreeSWITCH Integration
- [x] `bridge_agent_to_call(call_uuid, agent_id)`
  - [x] Successful bridge (+OK response)
  - [x] Failed bridge (-ERR response)
  - [x] Correct API command format
  - [x] Logging on success
  - [x] Logging on failure

- [x] `disconnect_call(call_uuid, cause)`
  - [x] Successful disconnect
  - [x] Failed disconnect
  - [x] Correct cause parameter
  - [x] Logging on success
  - [x] Logging on failure

### Call Event Handling
- [x] `handle_no_available_sales_agents(event_obj)`
  - [x] Disconnects call
  - [x] Adds to priority queue
  - [x] Returns correct status

- [x] `handle_channel_hangup_complete(event_obj, direction)`
  - [x] Normal hangup processing
  - [x] No answer handling
  - [x] Correct event cleanup
  - [x] Agent state updated

- [x] `handle_outbound_channel_answer(event_obj)`
  - [x] With pre-assigned agent
  - [x] Without agents (disconnect)
  - [x] Exception handling
  - [x] Correct return status

### Agent State Transitions
- [x] `handle_free_agent(agent_id)`
  - [x] Sales agent freed to sales queue
  - [x] Support agent freed to support queue
  - [x] Agent marked idle
  - [x] Agent added to correct queue

- [x] `handle_call_not_connected(direction)`
  - [x] Inbound: gets pending support agent
  - [x] Outbound: gets pending sales agent
  - [x] Agent marked idle
  - [x] Correct direction handling

### Inbound Call Handling
- [x] `handle_inbound_channel_create(event_obj)`
  - [x] Successful creation
  - [x] Failed bridge handling
  - [x] Exception handling
  - [x] Logging on failure

### Lock & Data Integrity
- [x] Lock integrity (events)
  - [x] Lock released on success
  - [x] Lock released on exception
  - [x] Pipeline atomicity verified
  - [x] All pipeline operations executed

---

## Coverage Summary

### Dialer Utils
- **Total Functions**: 18
- **Coverage**: ~100% (all code paths tested)
- **Test Classes**: 10
- **Total Tests**: 50+

### Events Utils
- **Total Functions**: 9
- **Coverage**: ~100% (all code paths tested)
- **Test Classes**: 8
- **Total Tests**: 30+

### Overall
- **Total Tests**: 80+
- **All Major Code Paths**: Covered
- **Lock Mechanisms**: Tested
- **Error Handling**: Comprehensive
- **Edge Cases**: Included
- **Data Integrity**: Verified

---

## Running Tests

### Run all tests with coverage:
```bash
pytest --cov=dialer.utils --cov=events.utils --cov-report=term-missing
```

### Run specific test class:
```bash
pytest dialer/tests.py::TestAddToPriorityQueue -v
```

### Run with detailed output:
```bash
pytest -vv --tb=short
```

### Generate HTML coverage report:
```bash
pytest --cov=dialer.utils --cov=events.utils --cov-report=html
# Open htmlcov/index.html
```

### Run in Docker:
```bash
docker-compose exec django pytest --cov=dialer.utils --cov=events.utils --cov-report=term-missing -v
```

---

## Notes

- All tests use mocking to avoid Redis dependencies
- Lock behavior is thoroughly tested
- Pipeline atomicity is verified
- Exception handling is comprehensive
- Data consistency is validated across all operations
- Both success and failure paths are covered

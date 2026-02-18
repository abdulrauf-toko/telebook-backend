"""
Unit tests for dialer/utils.py

Tests cover:
- Lock acquisition and release
- Redis data integrity
- Queue operations
- Agent state management
- Error handling and edge cases
"""

import pytest
import orjson as json
import time
from unittest.mock import Mock, MagicMock, patch, call
from dialer.utils import (
    get_priority_queue,
    add_to_priority_queue,
    get_all_idle_agents,
    get_all_idle_support_agents,
    get_all_idle_sales_agents,
    get_all_active_calls,
    get_all_active_sales_calls,
    remove_active_sales_call,
    remove_active_support_call,
    get_all_active_support_calls,
    add_sales_agent_to_queue,
    add_support_agent_to_queue,
    get_agent_state,
    get_all_agent_states,
    get_agent_team,
    get_next_available_sales_agent,
    get_next_available_support_agent,
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def mock_conn():
    """Mock Redis connection"""
    with patch('dialer.utils.conn') as mock:
        yield mock


@pytest.fixture
def mock_logger():
    """Mock logger"""
    with patch('dialer.utils.logger') as mock:
        yield mock


# ============================================================================
# TEST: get_priority_queue
# ============================================================================

class TestGetPriorityQueue:
    
    def test_get_empty_queue(self, mock_conn):
        """Test getting empty priority queue"""
        mock_conn.get.return_value = None
        
        result = get_priority_queue()
        
        assert result == []
        mock_conn.get.assert_called_once()
    
    def test_get_queue_with_data(self, mock_conn):
        """Test getting queue with valid data"""
        queue_data = [
            {"phone_number": "1234567890", "contact_id": 1},
            {"phone_number": "0987654321", "contact_id": 2},
        ]
        mock_conn.get.return_value = json.dumps(queue_data)
        
        result = get_priority_queue()
        
        assert result == queue_data
        assert len(result) == 2
    
    def test_get_queue_decode_error(self, mock_conn, mock_logger):
        """Test handling of JSON decode error"""
        mock_conn.get.return_value = "invalid json"
        
        result = get_priority_queue()
        
        assert result == []
        mock_logger.error.assert_called_once()


# ============================================================================
# TEST: add_to_priority_queue with Locks
# ============================================================================

class TestAddToPriorityQueue:
    
    def test_add_entry_success(self, mock_conn):
        """Test successfully adding entry to priority queue"""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.get.return_value = json.dumps([])
        
        entry = {"phone_number": "1234567890", "contact_id": 1}
        add_to_priority_queue(entry)
        
        # Verify lock was created and acquired
        mock_conn.lock.assert_called_once()
        mock_lock.acquire.assert_called_once()
        
        # Verify data was set
        mock_conn.set.assert_called_once()
        set_args = mock_conn.set.call_args[0]
        set_data = json.loads(set_args[1])
        assert entry in set_data
        
        # Verify lock was released
        mock_lock.release.assert_called_once()
    
    def test_add_entry_lock_timeout(self, mock_conn, mock_logger):
        """Test behavior when lock acquisition times out"""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        entry = {"phone_number": "1234567890"}
        add_to_priority_queue(entry)
        
        # Verify error was logged
        mock_logger.error.assert_called()
        assert "Could not acquire lock" in str(mock_logger.error.call_args)
        
        # Verify data was NOT set
        mock_conn.set.assert_not_called()
    
    def test_add_entry_exception_handling(self, mock_conn, mock_logger):
        """Test exception handling during add"""
        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = Exception("Redis error")
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        entry = {"phone_number": "1234567890"}
        add_to_priority_queue(entry)
        
        # Verify error was logged
        mock_logger.error.assert_called()
    
    def test_add_entry_appends_to_existing_queue(self, mock_conn):
        """Test that entry is appended to existing queue"""
        existing_queue = [{"phone_number": "9999999999", "contact_id": 5}]
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.get.return_value = json.dumps(existing_queue)
        
        new_entry = {"phone_number": "1111111111", "contact_id": 6}
        add_to_priority_queue(new_entry)
        
        # Verify new entry was appended
        set_args = mock_conn.set.call_args[0]
        set_data = json.loads(set_args[1])
        assert len(set_data) == 2
        assert set_data[0] == existing_queue[0]
        assert set_data[1] == new_entry


# ============================================================================
# TEST: Agent Queue Operations
# ============================================================================

class TestAgentQueueOperations:
    
    def test_add_sales_agent_to_queue(self, mock_conn):
        """Test adding sales agent to queue"""
        agent_id = "agent_123"
        
        add_sales_agent_to_queue(agent_id)
        
        # Verify zadd was called with correct key and agent
        mock_conn.zadd.assert_called_once()
        call_args = mock_conn.zadd.call_args
        assert call_args[0][0] == "SALES_AGENT_QUEUE"  # First arg is key
        assert agent_id in call_args[0][1]  # Second arg is dict with agent_id
    
    def test_add_support_agent_to_queue(self, mock_conn):
        """Test adding support agent to queue"""
        agent_id = "support_456"
        
        add_support_agent_to_queue(agent_id)
        
        mock_conn.zadd.assert_called_once()
        call_args = mock_conn.zadd.call_args
        assert call_args[0][0] == "SUPPORT_AGENT_QUEUE"
        assert agent_id in call_args[0][1]
    
    def test_get_all_idle_sales_agents(self, mock_conn):
        """Test retrieving all idle sales agents"""
        expected_agents = ["agent_1", "agent_2", "agent_3"]
        mock_conn.zrange.return_value = expected_agents
        
        result = get_all_idle_sales_agents()
        
        assert result == expected_agents
        mock_conn.zrange.assert_called_once()
    
    def test_get_all_idle_support_agents(self, mock_conn):
        """Test retrieving all idle support agents"""
        expected_agents = ["support_1", "support_2"]
        mock_conn.zrange.return_value = expected_agents
        
        result = get_all_idle_support_agents()
        
        assert result == expected_agents
    
    def test_get_all_idle_agents_combined(self, mock_conn):
        """Test retrieving all idle agents (combined)"""
        sales_agents = ["sales_1", "sales_2"]
        support_agents = ["support_1"]
        mock_conn.zrange.side_effect = [support_agents, sales_agents]
        
        result = get_all_idle_agents()
        
        assert result == support_agents + sales_agents
        assert mock_conn.zrange.call_count == 2


# ============================================================================
# TEST: Active Calls Operations
# ============================================================================

class TestActiveCallsOperations:
    
    def test_get_all_active_sales_calls(self, mock_conn):
        """Test retrieving all active sales calls"""
        calls = {
            "call_1": json.dumps({"agent_id": "agent_1", "duration": 120}),
            "call_2": json.dumps({"agent_id": "agent_2", "duration": 60}),
        }
        mock_conn.hgetall.return_value = calls
        
        result = get_all_active_sales_calls()
        
        assert result == calls
        mock_conn.hgetall.assert_called_once()
    
    def test_get_all_active_support_calls(self, mock_conn):
        """Test retrieving all active support calls"""
        calls = {
            "call_3": json.dumps({"agent_id": "support_1"}),
        }
        mock_conn.hgetall.return_value = calls
        
        result = get_all_active_support_calls()
        
        assert result == calls
    
    def test_get_all_active_calls_combined(self, mock_conn):
        """Test retrieving all active calls (combined)"""
        sales_calls = {"call_1": "data1"}
        support_calls = {"call_2": "data2"}
        mock_conn.hgetall.side_effect = [sales_calls, support_calls]
        
        result = get_all_active_calls()
        
        assert result == [sales_calls] + [support_calls]
        assert mock_conn.hgetall.call_count == 2
    
    def test_remove_active_sales_call_success(self, mock_conn):
        """Test removing active sales call"""
        call_id = "call_123"
        call_data = {"agent_id": "agent_1", "duration": 180}
        
        # Create a properly configured mock pipeline
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        # Handle orjson.dumps returning bytes
        encoded_data = json.dumps(call_data)
        if isinstance(encoded_data, bytes):
            encoded_data = encoded_data.decode('utf-8')
        mock_pipe.execute.return_value = [encoded_data, 1]
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_active_sales_call(call_id)
        
        assert result == call_data
        mock_pipe.hget.assert_called_once()
        mock_pipe.hdel.assert_called_once()
        mock_pipe.execute.assert_called_once()
    
    def test_remove_active_sales_call_not_found(self, mock_conn):
        """Test removing non-existent sales call"""
        call_id = "nonexistent"
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_pipe.execute.return_value = [None, 0]
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_active_sales_call(call_id)
        
        assert result is None
    
    def test_remove_active_support_call_success(self, mock_conn):
        """Test removing active support call"""
        call_id = "call_456"
        call_data = {"agent_id": "support_1"}
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        # Handle orjson.dumps returning bytes
        encoded_data = json.dumps(call_data)
        if isinstance(encoded_data, bytes):
            encoded_data = encoded_data.decode('utf-8')
        mock_pipe.execute.return_value = [encoded_data, 1]
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_active_support_call(call_id)
        
        assert result == call_data
    
    def test_remove_active_support_call_not_found(self, mock_conn):
        """Test removing non-existent support call"""
        call_id = "nonexistent"
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_pipe.execute.return_value = [None, 0]
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_active_support_call(call_id)
        
        assert result is None


# ============================================================================
# TEST: Agent State Operations
# ============================================================================

class TestAgentStateOperations:
    
    def test_get_agent_state(self, mock_conn):
        """Test retrieving agent state"""
        agent_id = "agent_1"
        agent_data = {"state": "idle", "current_call_id": None}
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        result = get_agent_state(agent_id)
        
        assert result == json.dumps(agent_data)
        mock_conn.hget.assert_called_once()
    
    def test_get_all_agent_states(self, mock_conn):
        """Test retrieving all agent states"""
        agents = {
            "agent_1": json.dumps({"state": "idle"}),
            "agent_2": json.dumps({"state": "busy"}),
        }
        mock_conn.get.return_value = agents
        
        result = get_all_agent_states()
        
        assert result == agents
    
    def test_get_agent_team(self, mock_conn):
        """Test retrieving agent team"""
        agent_id = "agent_1"
        agent_data = {"agent_team": "sales", "state": "idle"}
        
        # Mock get_agent_state behavior
        with patch('dialer.utils.get_agent_state') as mock_get_state:
            mock_get_state.return_value = agent_data
            
            result = get_agent_team(agent_id)
            
            assert result == "sales"


# ============================================================================
# TEST: Pending Agent Operations
# ============================================================================

class TestPendingAgentOperations:
    
    def test_get_pending_support_agent_found(self, mock_conn):
        """Test finding pending support agent"""
        agents_data = {
            "agent_1": {"state": "pending", "agent_team": "support"},
            "agent_2": {"state": "idle", "agent_team": "support"},
            "agent_3": {"state": "pending", "agent_team": "sales"},
        }
        mock_conn.get.return_value = agents_data
        
        with patch('dialer.utils.get_all_agent_states', return_value=agents_data):
            from dialer.utils import get_pending_support_agent
            result = get_pending_support_agent()
            
            assert result == "agent_1"
    
    def test_get_pending_support_agent_not_found(self, mock_conn):
        """Test when no pending support agent exists"""
        agents_data = {
            "agent_1": {"state": "idle", "agent_team": "support"},
            "agent_2": {"state": "busy", "agent_team": "support"},
        }
        mock_conn.get.return_value = agents_data
        
        with patch('dialer.utils.get_all_agent_states', return_value=agents_data):
            from dialer.utils import get_pending_support_agent
            result = get_pending_support_agent()
            
            assert result is None
    
    def test_get_pending_sales_agent_found(self, mock_conn):
        """Test finding pending sales agent"""
        agents_data = {
            "agent_1": {"state": "pending", "agent_team": "sales"},
            "agent_2": {"state": "idle", "agent_team": "sales"},
            "agent_3": {"state": "pending", "agent_team": "support"},
        }
        mock_conn.get.return_value = agents_data
        
        with patch('dialer.utils.get_all_agent_states', return_value=agents_data):
            from dialer.utils import get_pending_sales_agent
            result = get_pending_sales_agent()
            
            assert result == "agent_1"
    
    def test_get_pending_sales_agent_not_found(self, mock_conn):
        """Test when no pending sales agent exists"""
        agents_data = {
            "agent_1": {"state": "idle", "agent_team": "sales"},
            "agent_2": {"state": "busy", "agent_team": "sales"},
        }
        mock_conn.get.return_value = agents_data
        
        with patch('dialer.utils.get_all_agent_states', return_value=agents_data):
            from dialer.utils import get_pending_sales_agent
            result = get_pending_sales_agent()
            
            assert result is None


# ============================================================================
# TEST: Get Next Available Agent
# ============================================================================

class TestGetNextAvailableAgent:
    
    def test_get_next_available_sales_agent_success(self, mock_conn):
        """Test getting next available sales agent"""
        agent_id = "agent_1"
        mock_conn.zpopmin.return_value = [(agent_id, 123456.789)]
        
        result = get_next_available_sales_agent()
        
        assert result == agent_id
        mock_conn.zpopmin.assert_called_once_with("SALES_AGENT_QUEUE", count=1)
    
    def test_get_next_available_sales_agent_empty_queue(self, mock_conn):
        """Test getting agent when queue is empty"""
        mock_conn.zpopmin.return_value = []
        
        result = get_next_available_sales_agent()
        
        assert result is None
    
    def test_get_next_available_sales_agent_exception(self, mock_conn, mock_logger):
        """Test exception handling in get next available"""
        mock_conn.zpopmin.side_effect = Exception("Redis error")
        
        result = get_next_available_sales_agent()
        
        assert result is None
        mock_logger.exception.assert_called_once()
    
    def test_get_next_available_support_agent_success(self, mock_conn):
        """Test getting next available support agent"""
        agent_id = "support_1"
        mock_conn.zpopmin.return_value = [(agent_id, 123456.789)]
        
        result = get_next_available_support_agent()
        
        assert result == agent_id
    
    def test_get_next_available_support_agent_empty_queue(self, mock_conn):
        """Test getting support agent when queue is empty"""
        mock_conn.zpopmin.return_value = []
        
        result = get_next_available_support_agent()
        
        assert result is None
    
    def test_get_next_available_support_agent_exception(self, mock_conn, mock_logger):
        """Test exception handling in get next available support agent"""
        mock_conn.zpopmin.side_effect = Exception("Redis error")
        
        result = get_next_available_support_agent()
        
        assert result is None
        mock_logger.exception.assert_called_once()


# ============================================================================
# TEST: Lock Integrity and Concurrency
# ============================================================================

class TestLockIntegrity:
    
    def test_lock_is_released_on_success(self, mock_conn):
        """Verify lock is properly released on successful operation"""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.get.return_value = json.dumps([])
        
        add_to_priority_queue({"phone_number": "123"})
        
        # Lock should be released
        mock_lock.release.assert_called_once()
    
    def test_lock_is_released_on_error(self, mock_conn):
        """Verify lock is released even when exception occurs"""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_lock.release.side_effect = Exception("Release error")
        mock_conn.lock.return_value = mock_lock
        mock_conn.get.return_value = json.dumps([])
        
        # Should not raise, but catch exception
        with pytest.raises(Exception):
            add_to_priority_queue({"phone_number": "123"})
        
        # Attempt to release should have been made
        mock_lock.release.assert_called_once()
    
    def test_lock_timeout_configuration(self, mock_conn):
        """Verify lock timeout is properly configured"""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        add_to_priority_queue({"phone_number": "123"})
        
        # Verify lock was created with correct timeout
        call_args = mock_conn.lock.call_args
        assert "timeout" in call_args[1]
        assert "sleep" in call_args[1]


# ============================================================================
# TEST: Redis Data Integrity
# ============================================================================

class TestRedisDataIntegrity:
    
    def test_queue_data_remains_valid_after_add(self, mock_conn):
        """Verify queue data integrity after adding entry"""
        existing = [{"phone_number": "111", "id": 1}]
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.get.return_value = json.dumps(existing)
        
        new_entry = {"phone_number": "222", "id": 2}
        add_to_priority_queue(new_entry)
        
        # Verify the data set is valid JSON and correct
        set_call = mock_conn.set.call_args
        saved_data = json.loads(set_call[0][1])
        
        assert len(saved_data) == 2
        assert saved_data[0] == existing[0]
        assert saved_data[1] == new_entry
    
    def test_pipeline_ensures_atomicity(self, mock_conn):
        """Verify pipeline usage ensures atomic operations"""
        call_id = "call_123"
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        # Handle orjson.dumps returning bytes
        test_data = json.dumps({"data": "value"})
        if isinstance(test_data, bytes):
            test_data = test_data.decode('utf-8')
        mock_pipe.execute.return_value = [test_data, 1]
        mock_conn.pipeline.return_value = mock_pipe
        
        remove_active_sales_call(call_id)
        
        # Verify pipeline was used (context manager)
        mock_conn.pipeline.assert_called_once()
        
        # Verify execute was called (ensuring atomicity)
        mock_pipe.execute.assert_called_once()
    
    def test_json_encoding_decoding_consistency(self, mock_conn):
        """Verify JSON encoding/decoding doesn't lose data"""
        test_data = [
            {"phone": "123", "id": 1, "metadata": {"key": "value"}},
            {"phone": "456", "id": 2, "metadata": None},
        ]
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.get.return_value = json.dumps(test_data)
        
        add_to_priority_queue({"phone": "789", "id": 3})
        
        # Get the data that was saved
        saved = json.loads(mock_conn.set.call_args[0][1])
        
        # Verify original data is intact
        assert saved[0] == test_data[0]
        assert saved[1] == test_data[1]


# ============================================================================
# TEST: Edge Cases
# ============================================================================

class TestEdgeCases:
    
    def test_add_empty_entry(self, mock_conn):
        """Test adding empty entry to queue"""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.get.return_value = json.dumps([])
        
        add_to_priority_queue({})
        
        # Should still work
        mock_conn.set.assert_called_once()
    
    def test_add_large_entry(self, mock_conn):
        """Test adding large entry to queue"""
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.get.return_value = json.dumps([])
        
        large_entry = {
            "phone": "123456789",
            "metadata": {"large_field": "x" * 10000}
        }
        
        add_to_priority_queue(large_entry)
        
        mock_conn.set.assert_called_once()
    
    def test_multiple_calls_different_agents(self, mock_conn):
        """Test multiple operations with different agents"""
        agent_ids = ["agent_1", "agent_2", "agent_3"]
        
        for agent_id in agent_ids:
            add_sales_agent_to_queue(agent_id)
        
        # Verify all were added
        assert mock_conn.zadd.call_count == 3

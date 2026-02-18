"""
Unit tests for events/utils.py

Tests cover:
- Lock-based agent state management
- Pipeline atomicity
- Agent idle/busy state transitions
- Call bridging logic
- Error handling and edge cases
- FreeSWITCH integration
"""

import pytest
import orjson as json
import time
from unittest.mock import Mock, MagicMock, patch, call
from events.utils import (
    mark_agent_idle_in_cache,
    mark_agent_busy_in_cache,
    remove_agent_from_cache,
    add_active_call_in_cache,
    remove_active_call_in_cache,
    bridge_call,
    bridge_agent_to_call,
    disconnect_call,
    handle_no_available_sales_agents,
    handle_channel_hangup_complete,
    handle_outbound_channel_answer,
    handle_free_agent,
    handle_call_not_connected,
    handle_inbound_channel_create,
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def mock_conn():
    """Mock Redis connection"""
    with patch('events.utils.conn') as mock:
        yield mock


@pytest.fixture
def mock_logger():
    """Mock logger"""
    with patch('events.utils.logger') as mock:
        yield mock


@pytest.fixture
def mock_fs_manager():
    """Mock FreeSWITCH manager"""
    with patch('events.utils.fs_manager') as mock:
        yield mock


@pytest.fixture
def mock_event_obj():
    """Mock event object"""
    event = MagicMock()
    event.channel_uuid = "uuid_123"
    event.caller_id_number = "1234567890"
    event.caller_id_name = "John Doe"
    event.destination_number = "5551234567"
    event.channel_name = "sofia/internal/1001"
    event.event_id = "event_123"
    event.hangup_cause = "NORMAL_CLEARING"
    event.variable_bridge_to_uuid = "agent_1"
    return event


# ============================================================================
# TEST: mark_agent_idle_in_cache with Locks
# ============================================================================

class TestMarkAgentIdle:
    
    def test_mark_agent_idle_success_sales(self, mock_conn):
        """Test successfully marking sales agent as idle"""
        agent_id = "agent_1"
        agent_data = {"state": "busy", "team": "sales", "current_call_id": "call_123"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        result = mark_agent_idle_in_cache(agent_id)
        
        assert result is not None
        assert result["state"] == "idle"
        assert result["current_call_id"] is None
        mock_lock.acquire.assert_called_once()
        mock_lock.release.assert_called_once()
        mock_pipe.hset.assert_called_once()
        mock_pipe.zadd.assert_called_once()
    
    def test_mark_agent_idle_success_support(self, mock_conn):
        """Test successfully marking support agent as idle"""
        agent_id = "support_1"
        agent_data = {"state": "busy", "team": "support", "current_call_id": "call_456"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        result = mark_agent_idle_in_cache(agent_id)
        
        assert result is not None
        assert result["state"] == "idle"
        mock_pipe.zadd.assert_called_once()
    
    def test_mark_agent_idle_agent_not_found(self, mock_conn):
        """Test marking agent idle when agent doesn't exist"""
        agent_id = "nonexistent"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = None
        
        result = mark_agent_idle_in_cache(agent_id)
        
        assert result is None
    
    def test_mark_agent_idle_lock_timeout(self, mock_conn, mock_logger):
        """Test lock timeout when marking agent idle"""
        agent_id = "agent_1"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        result = mark_agent_idle_in_cache(agent_id)
        
        assert result is None
        mock_logger.error.assert_called()
    
    def test_mark_agent_idle_exception(self, mock_conn, mock_logger):
        """Test exception handling when marking agent idle"""
        agent_id = "agent_1"
        
        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = Exception("Redis error")
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        result = mark_agent_idle_in_cache(agent_id)
        
        assert result is None
        mock_logger.error.assert_called()


# ============================================================================
# TEST: mark_agent_busy_in_cache with Locks
# ============================================================================

class TestMarkAgentBusy:
    
    def test_mark_agent_busy_success_sales(self, mock_conn):
        """Test successfully marking sales agent as busy"""
        agent_id = "agent_1"
        call_id = "call_123"
        agent_data = {"state": "idle", "team": "sales"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        result = mark_agent_busy_in_cache(agent_id, call_id)
        
        assert result is True
        mock_lock.acquire.assert_called_once()
        mock_lock.release.assert_called_once()
        mock_pipe.hset.assert_called_once()
        mock_pipe.zrem.assert_called_once()
    
    def test_mark_agent_busy_success_support(self, mock_conn):
        """Test successfully marking support agent as busy"""
        agent_id = "support_1"
        call_id = "call_456"
        agent_data = {"state": "idle", "team": "support"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        result = mark_agent_busy_in_cache(agent_id, call_id)
        
        assert result is True
    
    def test_mark_agent_busy_agent_not_found(self, mock_conn):
        """Test marking agent busy when agent doesn't exist"""
        agent_id = "nonexistent"
        call_id = "call_123"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = None
        
        result = mark_agent_busy_in_cache(agent_id, call_id)
        
        assert result is None
    
    def test_mark_agent_busy_lock_timeout(self, mock_conn, mock_logger):
        """Test lock timeout when marking agent busy"""
        agent_id = "agent_1"
        call_id = "call_123"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        result = mark_agent_busy_in_cache(agent_id, call_id)
        
        assert result is None
        mock_logger.error.assert_called()
    
    def test_mark_agent_busy_exception(self, mock_conn, mock_logger):
        """Test exception handling when marking agent busy"""
        agent_id = "agent_1"
        call_id = "call_123"
        
        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = Exception("Redis error")
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        result = mark_agent_busy_in_cache(agent_id, call_id)
        
        assert result is None
        mock_logger.error.assert_called()


# ============================================================================
# TEST: remove_agent_from_cache with Locks
# ============================================================================

class TestRemoveAgentFromCache:
    
    def test_remove_agent_success_sales(self, mock_conn):
        """Test successfully removing sales agent from cache"""
        agent_id = "agent_1"
        agent_data = {"state": "idle", "team": "sales"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_agent_from_cache(agent_id)
        
        assert result is True
        mock_lock.acquire.assert_called_once()
        mock_lock.release.assert_called_once()
        mock_pipe.hdel.assert_called_once()
        mock_pipe.zrem.assert_called_once()
    
    def test_remove_agent_success_support(self, mock_conn):
        """Test successfully removing support agent from cache"""
        agent_id = "support_1"
        agent_data = {"state": "idle", "team": "support"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_agent_from_cache(agent_id)
        
        assert result is True
    
    def test_remove_agent_not_found(self, mock_conn):
        """Test removing agent that doesn't exist"""
        agent_id = "nonexistent"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = None
        
        result = remove_agent_from_cache(agent_id)
        
        assert result is None
    
    def test_remove_agent_lock_timeout(self, mock_conn, mock_logger):
        """Test lock timeout when removing agent"""
        agent_id = "agent_1"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        result = remove_agent_from_cache(agent_id)
        
        assert result is None
        mock_logger.error.assert_called()


# ============================================================================
# TEST: FreeSWITCH Integration
# ============================================================================

class TestFreeSwitchIntegration:
    
    def test_bridge_agent_to_call_success(self, mock_fs_manager, mock_logger):
        """Test successful bridge operation"""
        call_uuid = "call_123"
        agent_id = "agent_1"
        
        mock_result = MagicMock()
        mock_result.getBody.return_value = "+OK\n"
        mock_fs_manager.api.return_value = mock_result
        
        result = bridge_agent_to_call(call_uuid, agent_id)
        
        assert result is True
        mock_fs_manager.api.assert_called_once()
        mock_logger.info.assert_called_once()
    
    def test_bridge_agent_to_call_failure(self, mock_fs_manager, mock_logger):
        """Test failed bridge operation"""
        call_uuid = "call_123"
        agent_id = "agent_1"
        
        mock_result = MagicMock()
        mock_result.getBody.return_value = "-ERR\n"
        mock_fs_manager.api.return_value = mock_result
        
        result = bridge_agent_to_call(call_uuid, agent_id)
        
        assert result is False
    
    def test_disconnect_call_success(self, mock_fs_manager, mock_logger):
        """Test successful call disconnect"""
        call_uuid = "call_123"
        cause = "NORMAL_CLEARING"
        
        mock_result = MagicMock()
        mock_result.getBody.return_value = "+OK\n"
        mock_fs_manager.api.return_value = mock_result
        
        result = disconnect_call(call_uuid, cause)
        
        assert result is True
        mock_fs_manager.api.assert_called_once()
        mock_logger.info.assert_called_once()
    
    def test_disconnect_call_failure(self, mock_fs_manager, mock_logger):
        """Test failed call disconnect"""
        call_uuid = "call_123"
        cause = "NORMAL_CLEARING"
        
        mock_result = MagicMock()
        mock_result.getBody.return_value = "-ERR\n"
        mock_fs_manager.api.return_value = mock_result
        
        result = disconnect_call(call_uuid, cause)
        
        assert result is False
        mock_logger.error.assert_called_once()


# ============================================================================
# TEST: Handle Call Events
# ============================================================================

class TestHandleCallEvents:
    
    def test_handle_no_available_sales_agents(self, mock_event_obj, mock_fs_manager):
        """Test handling when no sales agents available"""
        mock_result = MagicMock()
        mock_result.getBody.return_value = "+OK\n"
        mock_fs_manager.api.return_value = mock_result
        
        with patch('events.utils.add_to_priority_queue') as mock_add_queue:
            result = handle_no_available_sales_agents(mock_event_obj)
            
            assert result["status"] == "disconnected"
            mock_add_queue.assert_called_once()
    
    def test_handle_channel_hangup_complete_normal_call(self, mock_event_obj):
        """Test handling normal call hangup"""
        mock_event_obj.hangup_cause = "NORMAL_CLEARING"
        
        with patch('events.utils.remove_active_sales_call') as mock_remove:
            with patch('events.utils.handle_free_agent') as mock_handle_free:
                mock_remove.return_value = {"call_id": "call_123"}
                
                handle_channel_hangup_complete(mock_event_obj, "outbound")
                
                mock_remove.assert_called_once()
                mock_handle_free.assert_called_once_with(mock_event_obj.variable_bridge_to_uuid)
    
    def test_handle_channel_hangup_complete_no_answer(self, mock_event_obj):
        """Test handling no answer call hangup"""
        mock_event_obj.hangup_cause = "NO_ANSWER"
        
        with patch('events.utils.handle_call_not_connected') as mock_handle_not_connected:
            handle_channel_hangup_complete(mock_event_obj, "outbound")
            
            mock_handle_not_connected.assert_called_once_with("outbound")
    
    def test_handle_outbound_channel_answer_with_agent(self, mock_event_obj):
        """Test handling outbound answer with pre-assigned agent"""
        mock_event_obj.get = MagicMock(return_value="agent_1")
        
        with patch('events.utils.bridge_agent_to_call', return_value=True):
            result = handle_outbound_channel_answer(mock_event_obj)
            
            assert result["status"] == "handled"
    
    def test_handle_outbound_channel_answer_no_agents(self, mock_event_obj, mock_logger):
        """Test handling outbound answer when no agents available"""
        mock_event_obj.get = MagicMock(return_value=None)
        
        with patch('events.utils.get_next_available_sales_agent', return_value=None):
            with patch('events.utils.handle_no_available_sales_agents') as mock_handle:
                result = handle_outbound_channel_answer(mock_event_obj)
                
                assert result["status"] == "disconnected"
                mock_handle.assert_called_once()
    
    def test_handle_outbound_channel_answer_exception(self, mock_event_obj, mock_logger):
        """Test exception handling in outbound answer"""
        mock_event_obj.get = MagicMock(side_effect=Exception("Error"))
        
        result = handle_outbound_channel_answer(mock_event_obj)
        
        assert result["status"] == "error"
        mock_logger.exception.assert_called_once()


# ============================================================================
# TEST: Agent State Management
# ============================================================================

class TestAgentStateManagement:
    
    def test_handle_free_agent_sales(self):
        """Test freeing sales agent"""
        agent_id = "agent_1"
        
        with patch('events.utils.mark_agent_idle_in_cache'):
            with patch('events.utils.get_agent_team', return_value='sales'):
                with patch('events.utils.add_sales_agent_to_queue') as mock_add:
                    handle_free_agent(agent_id)
                    
                    mock_add.assert_called_once_with(agent_id)
    
    def test_handle_free_agent_support(self):
        """Test freeing support agent"""
        agent_id = "support_1"
        
        with patch('events.utils.mark_agent_idle_in_cache'):
            with patch('events.utils.get_agent_team', return_value='support'):
                with patch('events.utils.add_support_agent_to_queue') as mock_add:
                    handle_free_agent(agent_id)
                    
                    mock_add.assert_called_once_with(agent_id)
    
    def test_handle_call_not_connected_inbound(self):
        """Test handling inbound call not connected"""
        agent_id = "support_1"
        
        with patch('events.utils.get_pending_support_agent', return_value=agent_id):
            with patch('events.utils.mark_agent_idle_in_cache') as mock_mark_idle:
                handle_call_not_connected("inbound")
                
                mock_mark_idle.assert_called_once_with(agent_id)
    
    def test_handle_call_not_connected_outbound(self):
        """Test handling outbound call not connected"""
        agent_id = "agent_1"
        
        with patch('events.utils.get_pending_sales_agent', return_value=agent_id):
            with patch('events.utils.mark_agent_idle_in_cache') as mock_mark_idle:
                handle_call_not_connected("outbound")
                
                mock_mark_idle.assert_called_once_with(agent_id)


# ============================================================================
# TEST: Inbound Call Handling
# ============================================================================

class TestInboundCallHandling:
    
    def test_handle_inbound_channel_create_success(self, mock_event_obj, mock_logger):
        """Test successful inbound channel creation"""
        agent_id = "support_1"
        
        with patch('events.utils.get_next_available_support_agent', return_value=agent_id):
            with patch('events.utils.bridge_agent_to_call', return_value=True):
                handle_inbound_channel_create(mock_event_obj)
                
                # Should not raise
                assert True
    
    def test_handle_inbound_channel_create_failure(self, mock_event_obj, mock_logger):
        """Test inbound channel creation failure"""
        agent_id = "support_1"
        
        with patch('events.utils.get_next_available_support_agent', return_value=agent_id):
            with patch('events.utils.bridge_agent_to_call', return_value=False):
                handle_inbound_channel_create(mock_event_obj)
                
                mock_logger.error.assert_called_once()
    
    def test_handle_inbound_channel_create_exception(self, mock_event_obj, mock_logger):
        """Test exception handling in inbound channel creation"""
        with patch('events.utils.get_next_available_support_agent', side_effect=Exception("Error")):
            handle_inbound_channel_create(mock_event_obj)
            
            mock_logger.exception.assert_called_once()


# ============================================================================
# TEST: Lock Integrity and Atomicity
# ============================================================================

class TestLockIntegrityEvents:
    
    def test_lock_released_on_success(self, mock_conn):
        """Verify lock is released on successful agent idle"""
        agent_id = "agent_1"
        agent_data = {"state": "busy", "team": "sales"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        mark_agent_idle_in_cache(agent_id)
        
        mock_lock.release.assert_called_once()
    
    def test_lock_released_on_exception(self, mock_conn):
        """Verify lock is released even on exception"""
        agent_id = "agent_1"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_lock.release.side_effect = Exception("Release error")
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = None
        
        mark_agent_idle_in_cache(agent_id)
        
        mock_lock.release.assert_called_once()
    
    def test_pipeline_atomicity_mark_idle(self, mock_conn):
        """Verify pipeline ensures atomicity for mark idle"""
        agent_id = "agent_1"
        agent_data = {"state": "busy", "team": "sales"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        mark_agent_idle_in_cache(agent_id)
        
        # Verify pipeline context manager was used
        mock_conn.pipeline.assert_called_once()
        # Verify execute was called
        mock_pipe.execute.assert_called_once()


# ============================================================================
# TEST: mark_agent_busy - Error Path Coverage (Lines 114-116, 124)
# ============================================================================

class TestMarkAgentBusyErrorPaths:
    
    def test_mark_agent_busy_raw_data_none_on_second_hget(self, mock_conn, mock_logger):
        """Test mark_agent_busy when agent logs out mid-operation (second hget returns None)"""
        agent_id = "agent_1"
        call_uuid = "call_123"
        
        # First hget returns valid data, second hget returns None (agent logged out)
        mock_conn.hget.side_effect = [
            json.dumps({"state": "idle", "team": "sales"}),  # First call
            None  # Second call - agent logged out
        ]
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        
        result = mark_agent_busy_in_cache(agent_id, call_uuid)
        
        assert result is None
        mock_logger.error.assert_called()
    
    def test_mark_agent_busy_lock_timeout(self, mock_conn, mock_logger):
        """Test mark_agent_busy when lock acquisition times out"""
        agent_id = "agent_1"
        call_uuid = "call_123"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False  # Lock acquisition fails
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        result = mark_agent_busy_in_cache(agent_id, call_uuid)
        
        assert result is None
        mock_logger.error.assert_called_once()


# ============================================================================
# TEST: remove_agent_from_cache - Error Path Coverage (Lines 128-138)
# ============================================================================

class TestRemoveAgentFromCacheErrorPaths:
    
    def test_remove_agent_agent_not_found(self, mock_conn, mock_logger):
        """Test remove_agent_from_cache when agent not found"""
        agent_id = "agent_1"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = None  # Agent not found
        
        result = remove_agent_from_cache(agent_id)
        
        assert result is None
    
    def test_remove_agent_lock_timeout(self, mock_conn, mock_logger):
        """Test remove_agent_from_cache when lock acquisition times out"""
        agent_id = "agent_1"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False  # Lock acquisition fails
        mock_lock.owned.return_value = False
        mock_conn.lock.return_value = mock_lock
        
        result = remove_agent_from_cache(agent_id)
        
        assert result is None
        mock_logger.error.assert_called()
    
    def test_remove_agent_exception_handling(self, mock_conn, mock_logger):
        """Test remove_agent_from_cache exception handling"""
        agent_id = "agent_1"
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_lock.release.side_effect = Exception("Release error")
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.side_effect = Exception("Redis error")
        
        result = remove_agent_from_cache(agent_id)
        
        assert result is None
        mock_logger.error.assert_called()
        mock_lock.release.assert_called_once()
    
    def test_remove_agent_support_team(self, mock_conn):
        """Test remove_agent_from_cache for support team"""
        agent_id = "support_1"
        agent_data = {"state": "idle", "team": "support"}
        
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.owned.return_value = True
        mock_conn.lock.return_value = mock_lock
        mock_conn.hget.return_value = json.dumps(agent_data)
        
        mock_pipe = MagicMock()
        mock_pipe.__enter__ = MagicMock(return_value=mock_pipe)
        mock_pipe.__exit__ = MagicMock(return_value=None)
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_agent_from_cache(agent_id)
        
        assert result is True
        mock_pipe.hdel.assert_called_once()
        mock_pipe.zrem.assert_called_once()


# ============================================================================
# TEST: Active Call Management (Lines 142-187)
# ============================================================================

class TestActiveCallManagement:
    
    def test_add_active_call_in_cache(self, mock_conn):
        """Test add_active_call_in_cache - currently a pass, verify it runs"""
        call_id = "call_123"
        details = {
            "agent_id": "agent_1",
            "caller_id": "1234567890"
        }
        
        result = add_active_call_in_cache(call_id, details)
        
        # Currently a pass, so should return None
        assert result is None
    
    def test_remove_active_call_in_cache_exists(self, mock_conn):
        """Test remove_active_call_in_cache when call exists"""
        call_id = "call_123"
        call_data = {
            "agent_id": "agent_1",
            "caller_id": "1234567890"
        }
        
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [json.dumps(call_data), 1]
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_active_call_in_cache(call_id)
        
        assert result == call_data
    
    def test_remove_active_call_in_cache_not_exists(self, mock_conn):
        """Test remove_active_call_in_cache when call doesn't exist"""
        call_id = "call_123"
        
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [None, 0]
        mock_conn.pipeline.return_value = mock_pipe
        
        result = remove_active_call_in_cache(call_id)
        
        assert result is None


# ============================================================================
# TEST: disconnect_call Error Path (Line 213)
# ============================================================================

class TestDisconnectCallErrorPath:
    
    def test_disconnect_call_failure(self, mock_fs_manager, mock_logger):
        """Test disconnect_call when FreeSWITCH API returns error"""
        call_uuid = "uuid_123"
        
        mock_api_response = MagicMock()
        mock_api_response.getBody.return_value = "-ERR No such channel"
        mock_fs_manager.api.return_value = mock_api_response
        
        result = disconnect_call(call_uuid)
        
        assert result is False
        mock_logger.error.assert_called_once()
    
    def test_disconnect_call_success(self, mock_fs_manager, mock_logger):
        """Test disconnect_call success path"""
        call_uuid = "uuid_123"
        
        mock_api_response = MagicMock()
        mock_api_response.getBody.return_value = "+OK"
        mock_fs_manager.api.return_value = mock_api_response
        
        result = disconnect_call(call_uuid)
        
        assert result is True
        mock_logger.info.assert_called_once()


# ============================================================================
# TEST: bridge_agent_to_call Error Paths
# ============================================================================

class TestBridgeAgentToCallErrorPaths:
    
    def test_bridge_agent_to_call_failure(self, mock_fs_manager):
        """Test bridge_agent_to_call when FreeSWITCH API fails"""
        call_uuid = "uuid_123"
        agent_id = "agent_1"
        
        mock_api_response = MagicMock()
        mock_api_response.getBody.return_value = "-ERR No such channel"
        mock_fs_manager.api.return_value = mock_api_response
        
        result = bridge_agent_to_call(call_uuid, agent_id)
        
        assert result is False
    
    def test_bridge_agent_to_call_success(self, mock_fs_manager, mock_logger):
        """Test bridge_agent_to_call success path"""
        call_uuid = "uuid_123"
        agent_id = "agent_1"
        
        mock_api_response = MagicMock()
        mock_api_response.getBody.return_value = "+OK"
        mock_fs_manager.api.return_value = mock_api_response
        
        result = bridge_agent_to_call(call_uuid, agent_id)
        
        assert result is True
        mock_logger.info.assert_called_once()


# ============================================================================
# TEST: handle_outbound_channel_answer Bridge Failure (Line 243)
# ============================================================================

class TestHandleOutboundChannelAnswerBridgeFailure:
    
    def test_handle_outbound_channel_answer_pre_assigned_bridge_failure(self, mock_event_obj, mock_logger):
        """Test handle_outbound_channel_answer when pre-assigned bridge fails"""
        agent_id = "agent_1"
        mock_event_obj.get.return_value = agent_id
        
        with patch('events.utils.bridge_agent_to_call', return_value=False):
            result = handle_outbound_channel_answer(mock_event_obj)
            
            assert result['status'] == 'error'
            assert 'failed bridging' in result['message']
    
    def test_handle_outbound_channel_answer_no_agent_available_bridge_fail(self, mock_event_obj, mock_logger):
        """Test handle_outbound_channel_answer when no agent but bridge fails"""
        mock_event_obj.get.return_value = None
        
        with patch('events.utils.get_next_available_sales_agent', return_value="agent_1"):
            with patch('events.utils.bridge_agent_to_call', return_value=False):
                result = handle_outbound_channel_answer(mock_event_obj)
                
                assert result['status'] == 'error'
                assert 'failed bridging' in result['message']
    
    def test_handle_outbound_channel_answer_exception(self, mock_event_obj, mock_logger):
        """Test handle_outbound_channel_answer exception handling"""
        mock_event_obj.get.side_effect = Exception("Error in get")
        
        result = handle_outbound_channel_answer(mock_event_obj)
        
        assert result['status'] == 'error'
        mock_logger.exception.assert_called_once()


# ============================================================================
# TEST: handle_inbound_channel_create Exception Handling (Lines 255-259)
# ============================================================================

class TestHandleInboundChannelCreateException:
    
    def test_handle_inbound_channel_create_get_agent_exception(self, mock_event_obj, mock_logger):
        """Test exception when getting next available support agent"""
        with patch('events.utils.get_next_available_support_agent', side_effect=Exception("DB Error")):
            handle_inbound_channel_create(mock_event_obj)
            
            mock_logger.exception.assert_called_once()
    
    def test_handle_inbound_channel_create_bridge_exception(self, mock_event_obj, mock_logger):
        """Test exception during bridge operation"""
        agent_id = "support_1"
        
        with patch('events.utils.get_next_available_support_agent', return_value=agent_id):
            with patch('events.utils.bridge_agent_to_call', side_effect=Exception("Bridge error")):
                handle_inbound_channel_create(mock_event_obj)
                
                mock_logger.exception.assert_called_once()

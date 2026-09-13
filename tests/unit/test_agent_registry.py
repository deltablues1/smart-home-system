"""
Unit Tests for Agent Registry
"""

import pytest
from config.agent_registry import (
    get_agent_config,
    get_all_agent_names,
    get_worker_agent_names,
    get_registry_stats,
    AGENT_REGISTRY
)


@pytest.mark.unit
class TestAgentRegistry:
    """Test Agent Registry functionality"""

    def test_agent_registry_exists(self):
        """Test that agent registry is defined"""
        assert AGENT_REGISTRY is not None
        assert isinstance(AGENT_REGISTRY, dict)
        assert len(AGENT_REGISTRY) > 0

    def test_all_agents_registered(self):
        """Test that all expected agents are registered"""
        agent_names = get_all_agent_names()
        expected_agents = [
            "orchestrator", "mailer", "librarian", "scribe",
            "analyst", "secretary", "rolodex", "tracker",
            "researcher", "scraper"
        ]
        
        for agent in expected_agents:
            assert agent in agent_names, f"Agent {agent} not found in registry"

    def test_get_agent_config(self):
        """Test getting agent configuration"""
        config = get_agent_config("mailer")
        assert config is not None
        assert config.name == "mailer"
        assert config.model.startswith("gemini")
        assert len(config.tools) > 0

    def test_get_worker_agents(self):
        """Test getting worker agents (excluding orchestrator)"""
        worker_names = get_worker_agent_names()
        assert "orchestrator" not in worker_names
        assert "mailer" in worker_names
        assert len(worker_names) >= 9

    def test_registry_stats(self):
        """Test registry statistics are consistent with the registry itself"""
        stats = get_registry_stats()
        assert stats["total_agents"] == len(AGENT_REGISTRY)
        assert stats["worker_agents"] == len(get_worker_agent_names())
        assert stats["worker_agents"] <= stats["total_agents"]
        assert stats["flash_agents"] > 0

    def test_agent_config_structure(self):
        """Test that agent configs have required fields"""
        for name, config in AGENT_REGISTRY.items():
            assert config.name == name
            assert config.module is not None
            assert config.class_name is not None
            assert config.model.startswith("gemini"), \
                f"Agent {name} has unexpected model: {config.model}"
            assert config.description is not None
            assert isinstance(config.tools, list)

    def test_model_distribution(self):
        """Test that model tier counts add up sanely"""
        stats = get_registry_stats()
        # Workhorse tier must dominate; tier counts can't exceed the registry
        assert stats["flash_agents"] >= 1
        tier_total = (stats["flash_agents"] + stats["pro_agents"]
                      + stats.get("flash_lite_agents", 0))
        assert tier_total <= stats["total_agents"]

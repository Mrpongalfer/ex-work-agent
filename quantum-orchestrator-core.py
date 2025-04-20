# quantum_orchestrator/core/agent.py
import json
import sys
import logging
import time
import asyncio
import signal
import traceback
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional, Callable, Union
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import importlib
import inspect
import uuid
import os

# Local imports
from .state_manager import StateManager
from .instruction_parser import InstructionParser
from .config import Config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [QuantumOrch] [%(levelname)-7s] [%(context)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Custom logger with context
class ContextLogger:
    def __init__(self, name="QuantumOrchestrator"):
        self.name = name
        self.logger = logging.getLogger(name)
        self.context = "main"
    
    def set_context(self, context):
        self.context = context
        
    def _log(self, level, msg, *args, **kwargs):
        # Add context to the log extra dict
        extra = kwargs.get('extra', {})
        extra['context'] = self.context
        kwargs['extra'] = extra
        getattr(self.logger, level)(msg, *args, **kwargs)
        
    def debug(self, msg, *args, **kwargs):
        self._log('debug', msg, *args, **kwargs)
        
    def info(self, msg, *args, **kwargs):
        self._log('info', msg, *args, **kwargs)
        
    def warning(self, msg, *args, **kwargs):
        self._log('warning', msg, *args, **kwargs)
        
    def error(self, msg, *args, **kwargs):
        self._log('error', msg, *args, **kwargs)
        
    def critical(self, msg, *args, **kwargs):
        self._log('critical', msg, *args, **kwargs)

# Global logger instance
logger = ContextLogger()

@dataclass
class ExecutionContext:
    """Stores context for an execution run"""
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    step_id: str = None
    description: str = ""
    start_time: float = field(default_factory=time.time)
    end_time: float = None
    success: bool = False
    actions_total: int = 0
    actions_completed: int = 0
    actions_succeeded: int = 0
    actions_failed: int = 0
    telemetry: Dict[str, Any] = field(default_factory=dict)
    
    def action_start(self):
        """Signal that an action is starting"""
        self.actions_total += 1
        
    def action_complete(self, success: bool):
        """Signal that an action has completed"""
        self.actions_completed += 1
        if success:
            self.actions_succeeded += 1
        else:
            self.actions_failed += 1
            
    def finalize(self, success: bool):
        """Finalize the execution context"""
        self.end_time = time.time()
        self.success = success
        
    @property
    def duration(self) -> float:
        """Get execution duration in seconds"""
        if self.end_time:
            return self.end_time - self.start_time
        return time.time() - self.start_time

class QuantumOrchestrator:
    """
    Advanced orchestration agent with neural flow pipeline, state awareness,
    and autonomous self-optimization.
    """
    
    def __init__(self, project_root: Optional[Path] = None):
        """Initialize the Quantum Orchestrator agent"""
        # Set project root (current directory if not specified)
        self.project_root = project_root or Path.cwd().resolve()
        
        # Initialize configuration
        self.config = Config(self.project_root)
        
        # Create state manager
        self.state_manager = StateManager(self.project_root)
        
        # Initialize instruction parser
        self.parser = InstructionParser()
        
        # Thread pool for parallel execution
        self.executor = ThreadPoolExecutor(max_workers=self.config.max_workers)
        
        # Dictionary to store action handlers
        self.action_handlers = {}
        
        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        
        # Initialization state
        self.initialized = False
        self.running = False
        
        # Load action handlers
        self._load_action_handlers()
        
        # Input sources (default to stdin)
        self.input_sources = []
        
        # Execution context stack
        self.execution_contexts = []
        
        # Mark as initialized
        self.initialized = True
        logger.info(f"Quantum Orchestrator initialized with project root: {self.project_root}")
    
    def _handle_shutdown(self, sig, frame):
        """Handle shutdown signals gracefully"""
        logger.info(f"Received shutdown signal {sig}, gracefully terminating...")
        self.running = False
        # Attempt to save state before exit
        self.state_manager.save_state()
        sys.exit(0)
    
    def _load_action_handlers(self):
        """Dynamically load all action handlers from handlers directory"""
        try:
            # Import handlers modules
            # In a real implementation, this would scan a handlers directory
            # For this example, we'll import specific modules
            from ..handlers import file_operations, execution, git_operations, llm_operations, quality_operations
            
            # Dictionary of handler modules
            handler_modules = {
                'file_operations': file_operations,
                'execution': execution,
                'git_operations': git_operations,
                'llm_operations': llm_operations,
                'quality_operations': quality_operations
            }
            
            # Register handlers from each module
            for module_name, module in handler_modules.items():
                # Find all handler functions in the module
                for name, obj in inspect.getmembers(module):
                    if name.startswith('handle_') and inspect.isfunction(obj):
                        action_name = name[7:].upper()  # Convert handle_create_file -> CREATE_FILE
                        self.action_handlers[action_name] = obj
                        logger.debug(f"Registered action handler: {action_name}")
                        
            logger.info(f"Loaded {len(self.action_handlers)} action handlers")
            
        except Exception as e:
            logger.error(f"Error loading action handlers: {e}")
            # Provide basic handlers for essential operations
            self._register_basic_handlers()
    
    def _register_basic_handlers(self):
        """Register basic handlers for essential operations"""
        from ..handlers.file_operations import handle_create_or_replace_file, handle_append_to_file
        from ..handlers.execution import handle_run_script, handle_echo
        
        # Register essential handlers
        self.action_handlers = {
            "ECHO": handle_echo,
            "CREATE_OR_REPLACE_FILE": handle_create_or_replace_file,
            "APPEND_TO_FILE": handle_append_to_file,
            "RUN_SCRIPT": handle_run_script,
        }
        logger.info(f"Registered {len(self.action_handlers)} basic action handlers")
    
    def add_input_source(self, source):
        """Add an input source for instructions"""
        self.input_sources.append(source)
    
    def process_instruction(self, instruction_json: str) -> Tuple[bool, dict]:
        """Process a single instruction"""
        # Create new execution context
        context = ExecutionContext()
        self.execution_contexts.append(context)
        
        # Set context ID for logging
        logger.set_context(f"run:{context.run_id[:8]}")
        
        try:
            # Parse the instruction
            instruction = self.parser.parse(instruction_json)
            
            # Extract metadata
            context.step_id = instruction.get('step_id', f"step-{len(self.execution_contexts)}")
            context.description = instruction.get('description', 'No description provided')
            
            logger.info(f"Processing instruction: {context.step_id} - {context.description}")
            
            # Check for dependencies
            if "depends_on" in instruction:
                # Check if dependencies have completed successfully
                dependencies = instruction["depends_on"]
                if not self._check_dependencies(dependencies):
                    logger.error(f"Dependencies not met for {context.step_id}")
                    context.finalize(False)
                    return False, {"error": "Dependencies not met", "context": context.__dict__}
            
            # Get actions to execute
            actions = instruction.get("actions", [])
            if not isinstance(actions, list):
                logger.error("Invalid instruction format: 'actions' must be a list")
                context.finalize(False)
                return False, {"error": "Invalid instruction format", "context": context.__dict__}
            
            # Create savepoint before execution
            savepoint = self.state_manager.create_savepoint(context.step_id)
            
            # Execute actions
            success = self._execute_actions(actions, context)
            
            # Finalize the context
            context.finalize(success)
            
            # If successful, commit the savepoint, otherwise rollback
            if success:
                self.state_manager.commit_savepoint(savepoint)
                logger.info(f"Successfully completed all actions for {context.step_id}")
                return True, {"success": True, "context": context.__dict__}
            else:
                self.state_manager.rollback_savepoint(savepoint)
                logger.error(f"Failed to complete actions for {context.step_id}")
                return False, {"error": "Action execution failed", "context": context.__dict__}
                
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON instruction: {e}")
            context.finalize(False)
            return False, {"error": f"JSON parsing error: {str(e)}", "context": context.__dict__}
        except Exception as e:
            logger.error(f"Unexpected error processing instruction: {e}", exc_info=True)
            context.finalize(False)
            return False, {"error": f"Unexpected error: {str(e)}", "context": context.__dict__}
    
    def _check_dependencies(self, dependencies: List[str]) -> bool:
        """Check if all dependencies have completed successfully"""
        for dep in dependencies:
            # Find the dependency in completed contexts
            found = False
            for ctx in self.execution_contexts[:-1]:  # Skip current context
                if ctx.step_id == dep:
                    found = True
                    if not ctx.success:
                        logger.error(f"Dependency {dep} failed")
                        return False
                    break
            
            if not found:
                logger.error(f"Dependency {dep} not found in completed steps")
                return False
        
        return True
    
    def _execute_actions(self, actions: List[dict], context: ExecutionContext) -> bool:
        """Execute a list of actions in the appropriate order"""
        # Analyze for dependencies between actions
        dependency_graph = self._build_action_dependency_graph(actions)
        
        # Execute actions according to dependency graph
        if self.config.parallel_execution and len(actions) > 1:
            return self._execute_actions_parallel(actions, dependency_graph, context)
        else:
            return self._execute_actions_sequential(actions, context)
    
    def _build_action_dependency_graph(self, actions: List[dict]) -> Dict[int, List[int]]:
        """Build a graph of action dependencies"""
        # Simple implementation - in a full system this would analyze 
        # resources used by each action to determine dependencies
        graph = {}
        for i in range(len(actions)):
            # For simplicity, actions depend on the previous action
            # A real implementation would use more sophisticated analysis
            graph[i] = [i-1] if i > 0 else []
        return graph
    
    def _execute_actions_sequential(self, actions: List[dict], context: ExecutionContext) -> bool:
        """Execute actions sequentially"""
        for i, action in enumerate(actions):
            action_num = i + 1
            action_type = action.get("type")
            
            # Set context for logging
            logger.set_context(f"run:{context.run_id[:8]}:action:{action_num}")
            logger.info(f"Executing action {action_num}/{len(actions)} (Type: {action_type})")
            
            # Mark action start in context
            context.action_start()
            
            # Get the handler for this action type
            handler = self.action_handlers.get(action_type)
            if handler:
                try:
                    # Execute the handler
                    start_time = time.time()
                    success, message = handler(action, self.project_root, self.state_manager)
                    duration = time.time() - start_time
                    
                    # Record telemetry
                    if action_type not in context.telemetry:
                        context.telemetry[action_type] = []
                    context.telemetry[action_type].append({
                        "duration": duration,
                        "success": success,
                        "action_num": action_num
                    })
                    
                    # Mark action completion in context
                    context.action_complete(success)
                    
                    if success:
                        logger.info(f"Action {action_num} '{action_type}' succeeded: {message}")
                    else:
                        logger.error(f"Action {action_num} '{action_type}' failed: {message}")
                        return False  # Stop on first failure
                except Exception as e:
                    logger.error(f"Error executing action {action_num} '{action_type}': {e}", exc_info=True)
                    context.action_complete(False)
                    return False
            else:
                logger.error(f"Unknown action type: '{action_type}'")
                context.action_complete(False)
                return False
        
        return True
    
    def _execute_actions_parallel(self, actions: List[dict], dependency_graph: Dict[int, List[int]], context: ExecutionContext) -> bool:
        """Execute actions in parallel based on dependency graph"""
        # This is a simplified implementation
        # A real implementation would use asyncio or similar for true parallelism
        
        action_results = [None] * len(actions)
        action_completed = [False] * len(actions)
        
        # Helper to check if an action can be executed
        def can_execute(action_idx):
            for dep_idx in dependency_graph[action_idx]:
                if not action_completed[dep_idx] or not action_results[dep_idx][0]:
                    return False
            return True
        
        # Execute actions until all are completed or one fails
        while not all(action_completed):
            for i, action in enumerate(actions):
                # Skip completed actions
                if action_completed[i]:
                    continue
                
                # Check if dependencies are satisfied
                if not can_execute(i):
                    continue
                
                # Execute the action
                action_type = action.get("type")
                logger.set_context(f"run:{context.run_id[:8]}:action:{i+1}")
                logger.info(f"Executing action {i+1}/{len(actions)} (Type: {action_type})")
                
                context.action_start()
                handler = self.action_handlers.get(action_type)
                
                if handler:
                    try:
                        start_time = time.time()
                        success, message = handler(action, self.project_root, self.state_manager)
                        duration = time.time() - start_time
                        
                        # Record telemetry
                        if action_type not in context.telemetry:
                            context.telemetry[action_type] = []
                        context.telemetry[action_type].append({
                            "duration": duration,
                            "success": success,
                            "action_num": i+1
                        })
                        
                        action_results[i] = (success, message)
                        action_completed[i] = True
                        context.action_complete(success)
                        
                        if success:
                            logger.info(f"Action {i+1} '{action_type}' succeeded: {message}")
                        else:
                            logger.error(f"Action {i+1} '{action_type}' failed: {message}")
                            # Mark all remaining actions as skipped
                            for j in range(i+1, len(actions)):
                                if not action_completed[j]:
                                    action_completed[j] = True
                                    action_results[j] = (False, "Skipped due to earlier failure")
                            return False
                    except Exception as e:
                        logger.error(f"Error executing action {i+1} '{action_type}': {e}", exc_info=True)
                        action_results[i] = (False, str(e))
                        action_completed[i] = True
                        context.action_complete(False)
                        return False
                else:
                    logger.error(f"Unknown action type: '{action_type}'")
                    action_results[i] = (False, f"Unknown action type: '{action_type}'")
                    action_completed[i] = True
                    context.action_complete(False)
                    return False
        
        return True
    
    def run_stdin_mode(self):
        """Run the agent in stdin mode (reading JSON from standard input)"""
        self.running = True
        logger.info("Quantum Orchestrator running in stdin mode")
        logger.info("Paste a JSON instruction block, press Enter, then send EOF (Ctrl+D or Ctrl+Z+Enter)")
        
        while self.running:
            print(f"\n{'-'*20} Ready for JSON instruction (Project: {self.project_root.name}) {'-'*20}")
            try:
                # Read potentially multi-line JSON until EOF
                json_input_lines = sys.stdin.readlines()  # Read all lines until EOF
                if not json_input_lines:
                    logger.info("EOF detected on empty input. Exiting.")
                    break
                
                json_input = "".join(json_input_lines)
                if not json_input.strip():
                    logger.warning("Received empty/whitespace input. Waiting for next instruction.")
                    continue
                
                logger.info(f"Received instruction ({len(json_input)} bytes). Processing...")
                success, result = self.process_instruction(json_input)
                
                if success:
                    print(f"\n✅ SUCCESS: Instruction processed successfully.")
                else:
                    print(f"\n❌ ERROR: Instruction processing failed. See logs for details.")
                
                # If we're running in interactive mode, continue to next instruction
                if self.config.interactive_mode:
                    continue
                else:
                    # In non-interactive mode, exit after processing one instruction
                    logger.info("Non-interactive mode: exiting after processing instruction.")
                    break
                    
            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received. Exiting.")
                break
            except Exception as e:
                logger.error(f"Critical error in main loop: {e}", exc_info=True)
                logger.error("Attempting to continue...")
                time.sleep(2)  # Avoid tight loop on error
    
    async def run_async(self):
        """Run the agent with all configured input sources"""
        self.running = True
        logger.info("Quantum Orchestrator running in async mode")
        
        # If no input sources were added, default to stdin
        if not self.input_sources:
            self.run_stdin_mode()
            return
        
        # Start all input sources
        input_tasks = []
        for source in self.input_sources:
            input_tasks.append(asyncio.create_task(source.start()))
        
        # Wait for all input sources to complete
        await asyncio.gather(*input_tasks)
    
    def run(self):
        """Main entry point - run the agent"""
        if not self.initialized:
            logger.error("Agent not properly initialized")
            return
        
        # If we have async input sources, use asyncio
        if self.input_sources:
            asyncio.run(self.run_async())
        else:
            # Otherwise, use stdin mode
            self.run_stdin_mode()

def main():
    """Main entry point for the Quantum Orchestrator agent"""
    # Project root is the current directory
    project_root = Path.cwd().resolve()
    
    # Create and run the agent
    agent = QuantumOrchestrator(project_root)
    
    # Execute based on command line arguments
    import argparse
    parser = argparse.ArgumentParser(description="Quantum Orchestrator - Advanced workflow automation agent")
    parser.add_argument("--watch", help="Watch a directory for instruction files", type=str)
    parser.add_argument("--api", help="Start the API server", action="store_true")
    parser.add_argument("--port", help="API server port", type=int, default=8080)
    parser.add_argument("--config", help="Path to config file", type=str)
    parser.add_argument("--interactive", help="Interactive mode", action="store_true", default=True)
    
    args = parser.parse_args()
    
    # Load config file if specified
    if args.config:
        agent.config.load_from_file(args.config)
    
    # Set interactive mode
    agent.config.interactive_mode = args.interactive
    
    # Configure input sources
    if args.watch:
        from ..automation.input_sources import FileWatcherSource
        watcher = FileWatcherSource(args.watch)
        agent.add_input_source(watcher)
    
    if args.api:
        from ..automation.input_sources import APISource
        api = APISource(port=args.port)
        agent.add_input_source(api)
    
    # Run the agent
    agent.run()

if __name__ == "__main__":
    main()

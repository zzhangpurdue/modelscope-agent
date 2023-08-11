import importlib
import traceback
from typing import Dict, List, Optional

from .llm import LLM
from .output_parser import MsOutputParser, OutputParser
from .output_wrapper import display
from .prompt import MSPromptGenerator, PromptGenerator
from .tools import DEFAULT_TOOL_LIST


class AgentExecutor:

    def __init__(self,
                 llm: LLM,
                 tool_cfg: Optional[Dict] = {},
                 additional_tool_list: Optional[Dict] = {},
                 prompt_generator: Optional[PromptGenerator] = None,
                 output_parser: Optional[OutputParser] = None):
        """
        the core class of ms agent. It is responsible for the interaction between user, llm and tools,
        and return the execution result to user.

        Args:
            llm (LLM): llm model, can be load from local or a remote server.
            tool_cfg (Optional[Dict]): cfg of default tools
            additional_tool_list (Optional[Dict], optional): user-defined additional tool list. Defaults to {}.
            prompt_generator (Optional[PromptGenerator], optional): this module is responsible for generating prompt
            according to interaction result. Defaults to use MSPromptGenerator.
            output_parser (Optional[OutputParser], optional): this module is responsible for parsing output of llm
            to executable actions. Defaults to use MsOutputParser.
        """
        self.llm = llm
        self._init_tools(tool_cfg, additional_tool_list)
        self.prompt_generator = prompt_generator or MSPromptGenerator()
        self.output_parser = output_parser or MsOutputParser()

        self.reset()

    def _init_tools(self,
                    tool_cfg: Dict = {},
                    additional_tool_list: Dict = {}):
        """init tool list of agent. We provide a default tool list, which is initialized by a cfg file.
        user can also provide user-defined tools by additional_tool_list.
        The key of additional_tool_list is tool name, and the value is corresponding object.

        Args:
            tool_cfg (Dict): default tool cfg.
            additional_tool_list (Dict, optional): user-defined tools. Defaults to {}.
        """
        self.tool_list = {}
        tool_list = DEFAULT_TOOL_LIST

        tools_module = importlib.import_module('modelscope_agent.tools')

        for task_name, tool_class_name in tool_list.items():
            tool_class = getattr(tools_module, tool_class_name)
            self.tool_list[task_name] = tool_class(tool_cfg)

        self.tool_list = {**self.tool_list, **additional_tool_list}
        self.available_tool_list = self.tool_list.keys()

    def set_available_tools(self, available_tool_list):

        if not set(available_tool_list).issubset(set(self.tool_list.keys())):
            raise ValueError('Unsupported tools found, please check')
        self.available_tool_list = available_tool_list

    def run(self, task: str, remote: bool = False) -> List[Dict]:
        """ use llm and tools to execute task given by user

        Args:
            task (str): concrete task
            remote (bool, optional): whether to execute tool in remote mode. Defaults to False.

        Returns:
            List[Dict]: execute result. One task may need to interact with llm multiple times,
            so a list of dict is returned. Each dict contains the result of one interaction.
        """

        self.prompt_generator.init_prompt(task, self.tool_list,
                                          self.available_tool_list)

        llm_result, exec_result = '', ''
        idx = 0
        final_res = []

        while True:
            idx += 1

            # generate prompt and call llm
            prompt = self.prompt_generator.generate(llm_result, exec_result)
            llm_result = self.llm.generate(prompt)
            print(f'|prompt{idx}: {prompt}')

            # display result
            display(llm_result, idx)

            # parse and get tool name and arguments
            action, action_args = self.output_parser.parse_response(llm_result)
            # print(f'|action: {action}, action_args: {action_args}')

            if action is None:
                # in chat mode, the final result of last instructions should be updated to prompt history
                _ = self.prompt_generator.generate(llm_result, '')

                return final_res

            if action in self.available_tool_list:
                action_args = self.parse_action_args(action_args)
                tool = self.tool_list[action]
                try:
                    exec_result = tool(**action_args, remote=remote)
                    # print(f'|exec_result: {exec_result}')

                    # parse exec result and store result to agent state
                    final_res.append(exec_result)
                    self.parse_exec_result(exec_result)
                except Exception:
                    exec_result = f'Action call error: {action}: {action_args}.'
                    traceback.print_exc()
                    return [{'error': exec_result}]
            else:
                exec_result = f"Unknown action: '{action}'. "
                return [{'error': exec_result}]

    def stream_run(self, task: str, remote: bool = True) -> Dict:
        """this is a stream version of run, which can be used in scenario like gradio.
        It will yield the result of each interaction, so that the caller can display the result

        Args:
            task (str): concrete task
            remote (bool, optional): whether to execute tool in remote mode. Defaults to True.

        Yields:
            Iterator[Dict]: iterator of llm response and tool execution result
        """

        self.prompt_generator.init_prompt(task, self.tool_list,
                                          self.available_tool_list)

        llm_result, exec_result = '', ''
        idx = 0

        while True:
            idx += 1
            prompt = self.prompt_generator.generate(llm_result, exec_result)
            print(f'|prompt{idx}: {prompt}')

            llm_result = ''
            try:
                for s in self.llm.stream_generate(prompt):
                    llm_result += s
                    yield {'llm_text': s}

            except Exception:
                raise NotImplementedError(
                    'This llm does not implement stream predict')
                return

            # parse and get tool name and arguments
            action, action_args = self.output_parser.parse_response(llm_result)

            if action is None:
                # in chat mode, the final result of last instructions should be updated to prompt history
                prompt = self.prompt_generator.generate(llm_result, '')
                return

            if action in self.available_tool_list:
                action_args = self.parse_action_args(action_args)
                tool = self.tool_list[action]
                try:
                    exec_result = tool(**action_args, remote=remote)
                    yield {'exec_result': exec_result}

                    # parse exec result and update state
                    self.parse_exec_result(exec_result)
                except Exception as e:
                    raise e
                    return
            else:
                exec_result = f"Unknown action: '{action}'. "
                yield {'exec_result': exec_result}

    def reset(self):
        """
        clear history and agent state
        """
        self.prompt_generator.reset()
        self.agent_state = {}

    def parse_action_args(self, action_args):
        """
        replace action_args in str to Image/Video/Audio Wrapper, so that tool can handle them
        """
        parsed_action_args = {}
        for name, arg in action_args.items():
            try:
                true_arg = self.agent_state.get(arg, arg)
            except Exception:
                true_arg = arg
            parsed_action_args[name] = true_arg
        return parsed_action_args

    def parse_exec_result(self, exec_result, *args, **kwargs):
        """
        update exec result to agent state.
        key is the str representation of the result.
        """
        for k, v in exec_result.items():
            self.agent_state[str(v)] = v
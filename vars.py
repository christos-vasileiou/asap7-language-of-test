models_causal = {
  'tiny-mistral': "openaccess-ai-collective/tiny-mistral",
  'llama-2': "meta-llama/Llama-2-7b-chat-hf",
  'nousre-llama-2': "NousResearch/Llama-2-7b-chat-hf",
  'llama-2-atpg': "chrivasileiou/LlamaModelForCausalLM-ATPG",
  'llama-2-combin-atpg': "chrivasileiou/LlamaModelForCausalLM-Combin-ATPG",
  'llama-2-atpg-lora': "chrivasileiou/LlamaModelForCausalLM-ATPG-LoRA",
  'llama-2-combin-atpg-lora': "chrivasileiou/LlamaModelForCausalLM-Combin-ATPG-LoRA",
  'codegemma-2b': "google/codegemma-2b",
  'codegemma-7b': "google/codegemma-7b-it",
}

models_seq2seq = {
  't5-tiny': "google/t5-efficient-tiny",
  't5-l': "google-t5/t5-large",
  't5-xl': "google-t5/t5-3b",
}

# These circuits will be provided in structural verilog. You need to pay attention to the context within the tokens [INST] and [/INST]. The context will be your instruction. Then, think about the reasoning process in your mind and lastly provide your best answer. The thinking process should be reported after the CHAIN_OF_THOUGHT tag. Your answer will contain the simulation, the input test vector, the output test vector and the detected faults list, which will be provided after the SNAPSHOT, INPUT_VECTOR, EXPECTED_OUTPUT and DETECTED_FAULTS tags, respectively. 
_system_prompts = [
  "You are an ATPG tool used to generate test patterns for structural Verilog netlists.",
]

_user_prompt_dict = {
                    'system_content': '',
                    'user_content': '',
                    'assistant_content': '',
                    'module_name': '',
                    'netlist': '',
                    'input_vector': '',
                    'expected_output': '',
                    'snapshot': '',
                    'detected_faults': '',
                    }

# Faults List
_training_prompts_faults_list = [
  "Create a test vector for the circuit \"{module_name}\" to detect the \"{fault}\" in the following netlist:\n```verilog\n{netlist}\n```\n",
  "Please generate a test pattern that targets the \"{fault}\" for the circuit \"{module_name}\" using this netlist:\n```verilog\n{netlist}\n```\n",
  "Develop a test vector aimed at covering the \"{fault}\" in the circuit \"{module_name}\" based on the netlist below:\n```verilog\n{netlist}\n```\n",
  "For the module \"{module_name}\", design a test vector that identifies the \"{fault}\" within this netlist:\n```verilog\n{netlist}\n```\n",
  "I need a test pattern for the circuit \"{module_name}\" that can uncover the \"{fault}\" as specified in the netlist:\n```verilog\n{netlist}\n```\n",
  "Generate a test vector targeting the \"{fault}\" for the circuit named \"{module_name}\" using the provided netlist:\n```verilog\n{netlist}\n```\n",
  "Can you write a test pattern for \"{module_name}\" that covers the \"{fault}\" in the following netlist?\n```verilog\n{netlist}\n```\n",
  "Please formulate a test vector for the circuit \"{module_name}\" to address the \"{fault}\" based on this netlist:\n```verilog\n{netlist}\n```\n",
  "Construct a test pattern for \"{module_name}\" that targets the \"{fault}\" using the given netlist:\n```verilog\n{netlist}\n```\n",
  "Design a test vector for the module \"{module_name}\" aimed at detecting the \"{fault}\" in the subsequent netlist:\n```verilog\n{netlist}\n```\n",
]


_cot_assistant_response_faults_list = ["""CHAIN_OF_THOUGHT:
1. **Analyze the Netlist**: Examine the provided netlist to understand the circuit's structure and identify components related to the {fault}.
2. **Determine the Fault Characteristics**: Understand how the {fault} manifests within the circuit and which components are affected.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Generate the Input Vector**: Create an input vector that specifically targets the {fault}, ensuring it activates the faulty behavior.
5. **Predict the Expected Output**: Based on the input vector and the nature of the {fault}, determine the expected output when the fault is present.
6. **Run the Circuit Simulation**: Use the input vector to simulate the circuit and capture the actual behavior, generating the simulation snapshot.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Examine the Netlist**: Review the netlist to identify key components and pathways related to the {fault}.
2. **Understand Fault Mechanism**: Analyze how the {fault} affects the circuit's performance and which signals are impacted.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Develop the Test Input**: Formulate an input vector that will induce the {fault}, ensuring it triggers the specific faulty behavior.
5. **Establish Expected Behavior**: Define what the circuit's output should be when the {fault} is correctly triggered by the input vector.
6. **Perform Circuit Simulation**: Execute a simulation with the input vector to observe the actual output and generate a snapshot of the circuit's state.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Review Netlist Details**: Analyze the netlist to comprehend the circuit's configuration and pinpoint areas susceptible to the {fault}.
2. **Characterize the Fault**: Understand the nature of the {fault} and its impact on circuit operations.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Craft the Input Vector**: Design an input vector that specifically activates the {fault}, ensuring it influences the targeted components.
5. **Define Expected Output**: Predict the output that should result from the input vector if the {fault} is present.
6. **Conduct Simulation**: Run a simulation using the input vector to obtain a snapshot of the circuit's actual behavior.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Netlist Examination**: Inspect the netlist to identify components and connections relevant to the {fault}.
2. **Fault Analysis**: Determine how the {fault} affects signal flow and circuit functionality.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Input Vector Formulation**: Create an input vector that induces the {fault} by interacting with the affected components.
5. **Expected Output Specification**: Establish the expected output behavior when the {fault} is active.
6. **Simulation Execution**: Perform a simulation with the input vector to capture the circuit's behavior, resulting in a snapshot.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Analyze Circuit Structure**: Review the netlist to understand the layout and identify sections related to the {fault}.
2. **Understand Fault Impact**: Examine how the {fault} disrupts normal circuit operations and which signals are affected.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Design Test Input**: Develop an input vector that activates the {fault}, ensuring it interacts with the relevant components.
5. **Determine Expected Outcome**: Predict the output that should result from the input vector if the {fault} is present.
6. **Simulate Circuit Behavior**: Run a simulation using the input vector to generate a snapshot of the circuit's actual response.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Netlist Review**: Examine the provided netlist to identify components and connections pertinent to the {fault}.
2. **Fault Mechanism Analysis**: Understand how the {fault} affects the circuit's behavior and which pathways are involved.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Input Vector Creation**: Formulate an input vector that specifically targets and induces the {fault} within the circuit.
5. **Expected Output Determination**: Define the expected output that should result when the {fault} is activated by the input vector.
6. **Circuit Simulation**: Execute a simulation using the input vector to observe the circuit's behavior and capture the snapshot.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Examine Netlist Components**: Analyze the netlist to identify parts of the circuit that are related to the {fault}.
2. **Determine Fault Influence**: Assess how the {fault} impacts signal flow and overall circuit performance.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Develop Input Vector**: Create an input vector that will trigger the {fault}, ensuring it interacts with the affected components.
5. **Predict Expected Output**: Establish what the output should be when the input vector activates the {fault}.
6. **Simulate Circuit Response**: Run a simulation with the input vector to generate a snapshot of the circuit's actual output.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Netlist Analysis**: Inspect the netlist to identify components and connections that could be affected by the {fault}.
2. **Fault Impact Evaluation**: Understand how the {fault} alters the circuit's behavior and which signals are disrupted.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Input Vector Formulation**: Design an input vector that specifically induces the {fault}, targeting the affected areas.
5. **Expected Output Specification**: Define the expected output when the {fault} is present and the input vector is applied.
6. **Conduct Simulation**: Perform a simulation using the input vector to observe the actual output and capture the snapshot.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Review Netlist for Fault Analysis**: Analyze the netlist to pinpoint components and connections that are susceptible to the {fault}.
2. **Understand Fault Dynamics**: Examine how the {fault} affects the circuit's operation and which signals it disrupts.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Create Input Vector**: Develop an input vector that specifically targets the {fault}, ensuring it activates the faulty behavior.
5. **Determine Expected Output**: Predict the output that should result when the input vector successfully induces the {fault}.
6. **Simulate Circuit Behavior**: Run a simulation with the input vector to observe the circuit's response and capture the simulation snapshot.
SNAPSHOT:
{snapshot}
""",
"""CHAIN_OF_THOUGHT:
1. **Identify Relevant Components in Netlist**: Examine the netlist to locate components and connections related to the {fault}.
2. **Analyze Fault Mechanism**: Understand how the {fault} impacts the circuit's functionality and which parts are affected.
3. **Simulate the Fault**: Perform a fault simulation to observe how the {fault} impacts the circuit's behavior.
4. **Formulate Input Vector**: Create an input vector that effectively induces the {fault} by interacting with the identified components.
5. **Predict Expected Output**: Define the expected output when the input vector triggers the {fault}.
6. **Execute Simulation**: Perform a simulation using the input vector to capture the circuit's behavior, resulting in a snapshot.
SNAPSHOT:
{snapshot}
"""
]

_answer_template = ["""
INPUT_VECTOR: "{input_vector}"
EXPECTED_OUTPUT: "{expected_output}"
DETECTED_FAULTS: "{detected_faults}"
"""]



_extras = [
  "",
  "Please outline the test vectors step by step, ensuring coverage for a range of possible issues.",
  "Describe the test vectors you would write for this IC, detailing the types of faults and defects each vector is intended to uncover.",
  "Develop test vectors specifically aimed at uncovering these stuck-at faults.",
  "Focus on the key principles and methodologies that should be considered in the test vector development process.",
  "Please think carefully.",
]


chat_template = """{% for message_group in messages %}
  {% if message_group[0]['role'] == 'system' %}
    {% set loop_messages = message_group[1:] %}
    {% set system_message = message_group[0]['content'] %}
  {% else %}
    {% set loop_messages = message_group %}
    {% set system_message = false %}
  {% endif %}
  {% for message in loop_messages %}
    {% if (message['role'] == 'user') != (loop.index0 % 2 == 0) %}
      {{ raise_exception('Conversation roles must alternate user/assistant/user/assistant/...') }}
    {% endif %}
    {% if loop.index0 == 0 and system_message != false %}
      {% set content = '<<SYS>>\n' + system_message + '\n<</SYS>>\n\n' + message['content'] %}
    {% else %}
      {% set content = message['content'] %}
    {% endif %}
    {% if message['role'] == 'user' %}
      {{ bos_token + '[INST] ' + content.strip() + ' [/INST]' }}
    {% elif message['role'] == 'assistant' %}
      {{ ' '  + content.strip() + ' ' + eos_token }}
    {% endif %}
  {% endfor %}
{% endfor %}
"""

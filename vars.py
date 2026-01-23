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
  "You are an ATPG assistant used to generate test patterns for structural Verilog netlists.",
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
  "Create a test vector for the circuit \"{module_name}\" to detect the \"{fault}\" in the following netlist:\n{netlist}\n\n",
  "Please generate a test pattern that targets the \"{fault}\" for the circuit \"{module_name}\" using this netlist\n```\n{netlist}\n```\n",
  "Develop a test vector aimed at covering the \"{fault}\" in the circuit \"{module_name}\" based on the netlist below:\n```verilog\n{netlist}\n```\n",
  "For the module \"{module_name}\", design a test vector that identifies the \"{fault}\" within this netlist\n\n{netlist}",
  "I need a test pattern for the circuit \"{module_name}\" that can uncover the \"{fault}\" as specified in the netlist:\n\n{netlist}",
  "Generate a test vector targeting the \"{fault}\" for the circuit named \"{module_name}\" using the provided netlist {netlist}",
  "Can you write a test pattern for \"{module_name}\" that covers the \"{fault}\" in the following netlist?\n\n{netlist}",
  "Please formulate a test vector for the circuit \"{module_name}\" to address the \"{fault}\" based on this netlist:\n```\n{netlist}\n```\n",
  "Construct a test pattern for \"{module_name}\" that targets the \"{fault}\" using the given netlist:\n\n{netlist}",
  "Design a test vector for the module \"{module_name}\" aimed at detecting the \"{fault}\" in the subsequent netlist\n\n{netlist}",
]


_cot_assistant_response_faults_list = [
"""1. **Analyze the Netlist**: Identify the target net {fault_net} and its driving/driven gates within the module {module_name}.
2. **Excitation**: To detect a {fault_model_long} on {fault_net}, determine the required primary input values to drive the net to {excitation_value}. The controlling nets from primary inputs are {primary_controlling_nets}.
3. **Fault Propagation Path**: Analyzing the paths, especially through the {propagation_gates} to {primary_observation_nets}, reveals the key conditions for fault propagation. Identify a sensitive path from {fault_net} to observable outputs {primary_observation_nets}. 
4. **Side-Input Justification (Backtrack)**: For the fault to propagate through the gates, we trace back via {backtrack_gates} and justify the non-controlling values for side-inputs: {non_controlling_nets}.
5. **Conflict Check**: Verify if the assignments for excitation and propagation are logically consistent across the logic cone. 
6. **Expected Output (D-Frontier)**: Predict the good machine output vs. the faulty machine output at {primary_observation_nets}. Expected output for good machine: {expected_output}.
7. **Simulation Snapshot**: Generate the final logic state for all internal wires to confirm the test vector {input_vector}. Detected faults from the injected location and forward: {detected_faults}.""",
"""1. **Fault Location**: Locate {fault_net} in the structural netlist of module {module_name}. It is tied to the input of gates in the propagation path.
2. **Setup Excitation**: Force {fault_net} to {excitation_value} by assigning primary controlling inputs {primary_controlling_nets}. This targets the {fault_model_short} fault.
3. **Sensitize Propagation Gates**: Analyzing the paths, especially through the {propagation_gates} to {primary_observation_nets}, reveals the key conditions for fault propagation. The fault effect (D or D-bar) is at the input of {propagation_gates}. To propagate, the gate's other inputs must be held at non-controlling values: {non_controlling_nets}.
4. **Backtracking Logic**: Trace back through {backtrack_gates} to primary inputs to ensure no logic conflicts exist with the excitation requirements.
5. **D-Frontier Advancement**: Observe the fault effect at the output of the circuit. The discrepancy should be visible at {primary_observation_nets}.
6. **Fault Simulation**: Compare the behavior of the good machine (expected output: {expected_output}) vs. the faulty model. The {fault_model_long} should manifest at the outputs.
7. **Snapshot Capture**: Record the nodal logic levels for verification with input vector {input_vector}. Detected faults: {detected_faults}.""",
"""1. **Structural Analysis**: Map the logic cone of {fault_net} in module {module_name}. It feeds into the propagation gates.
2. **Fault Activation**: Set primary inputs via {primary_controlling_nets} to ensure {fault_net} is driven to {excitation_value}. Target fault: {fault_model_short}.
3. **Path Selection**: Select the shortest path to an output: {fault_net} -> {propagation_gates} -> {primary_observation_nets}.
4. **Gate Constraints**: For each gate in the propagation path, set non-controlling nets: {non_controlling_nets}.
5. **Backtrack Assignment**: Solve the Boolean satisfiability for the required side-inputs using backtrack gates {backtrack_gates}.
6. **Output Prediction**: If {fault_net} has {fault_model_long}, the output at {primary_observation_nets} will differ. Expected output: {expected_output}.
7. **Simulation Capture**: Execute the input vector {input_vector} and snapshot the state. Detected faults: {detected_faults}.""",
"""1. **Target Objective**: Define the initial objective to detect {fault_model_short} at {fault_net} in module {module_name}.
2. **Backtrace to PIs**: Trace backward from {fault_net} through {backtrack_gates} to find the Primary Input assignments needed to satisfy {fault_net} = {excitation_value}. Controlling nets: {primary_controlling_nets}.
3. **Implication Check**: Perform a forward implication from the PI assignments to see if they create any logic conflicts or inadvertently mask the fault at the propagation gates.
4. **D-Frontier Maintenance**: Identify the current 'D' or 'D-bar' at {propagation_gates}. To move the frontier, justify side-inputs to their non-controlling values: {non_controlling_nets}.
5. **Path Sensitization**: Ensure the sensitized path extends from the propagation gates to the Primary Outputs {primary_observation_nets}.
6. **Verification**: Confirm that the final input vector {input_vector} results in the expected output {expected_output} for the fault-free circuit (detecting {fault_model_long}).
7. **Execution**: Generate the logic snapshot. Detected faults as the fault propagates forward: {detected_faults}.""",
"""1. **Fault Model Analysis**: Analyze the {fault_model_long} behavior on net {fault_net} within module {module_name}.
2. **Primary Input Justification**: Determine the specific PI values via {primary_controlling_nets} required to excite the fault with {excitation_value}. 
3. **Controllability and Observability**: Evaluate the controllability of {fault_net} and the observability of the path leading to {primary_observation_nets}.
4. **Gate-Level Propagation**: At gates {propagation_gates}, the fault signal ({fault_model_short}) must be propagated. The remaining inputs are set to non-controlling values: {non_controlling_nets}.
5. **Backtrack and Resolve**: If a conflict occurs, backtrack through {backtrack_gates} to the previous decision point and re-assign alternative values.
6. **Differential Analysis**: Compare the output at {primary_observation_nets}: Good machine produces {expected_output}, faulty machine differs.
7. **State Capture**: Output the simulation snapshot with input vector {input_vector}. Detected faults: {detected_faults}.""",
"""1. **Circuit Topology Scan**: Review the netlist for module {module_name} to find the cone of influence for {fault_net}.
2. **Excitation Strategy**: Assign values to {primary_controlling_nets} to force {fault_net} to {excitation_value}, contrasting the {fault_model_short} state.
3. **Sensitizing the Path**: To propagate the fault effect ({fault_model_long}) to {primary_observation_nets}, analyze the series of gates {propagation_gates}. 
4. **Justification of Side-Inputs**: For each gate in the propagation path, justify the off-path inputs to ensure they are non-controlling: {non_controlling_nets}.
5. **Conflict Resolution**: Check if the required values for propagation conflict with the required values for excitation. Use {backtrack_gates} to resolve.
6. **Expected Signature**: Identify the expected output at {primary_observation_nets}: {expected_output}.
7. **Snapshot Generation**: Document the logic state of all wires with input vector {input_vector}. Detected faults starting from the location where the fault occurred and onwards: {detected_faults}.""",
"""1. **Identify Fault Site**: Target {fault_net} for a {fault_model_long} test in module {module_name}.
2. **Excitation of the Fault**: Select PI assignments via {primary_controlling_nets} that result in {fault_net} being {excitation_value}.
3. **Forward Propagation (D-Algorithm)**: Place a 'D' (or 'D-bar') on the fault site. Propagate this value to the output by setting the side-inputs of {propagation_gates} to non-controlling wires: {non_controlling_nets}.
4. **Consistency Check (Backward Trace)**: Ensure that the values required for propagation are consistent with the PI assignments made for excitation. Backtrack through {backtrack_gates} if needed.
5. **Observation Point**: Target the outputs {primary_observation_nets} as the primary observation points for the {fault_model_short} fault.
6. **Fault Discrimination**: Verify that the output logic differs between the healthy machine (expected: {expected_output}) and the faulty machine.
7. **Simulation Snapshot**: Provide the full circuit state with input vector {input_vector}. Detected faults that the fault causes and forward: {detected_faults}.""",
"""1. **Initialization**: Load the structural Verilog for {module_name} and target the {fault_model_short} on net {fault_net}.
2. **Logic Justification**: To excite the {fault_model_long} fault, we need {fault_net} at {excitation_value}. This requires setting {primary_controlling_nets} appropriately.
3. **Propagation Logic**: The fault effect must pass through {propagation_gates}. We must ensure that side-inputs have non-controlling values: {non_controlling_nets}.
4. **Recursive Backtracking**: If the required values cannot be satisfied by the PIs, backtrack through {backtrack_gates} and try alternative assignments.
5. **Output Verification**: The fault is successfully sensitized if {primary_observation_nets} shows a logic discrepancy from {expected_output}.
6. **Final Vector Synthesis**: The combined assignments result in the input vector {input_vector}.
7. **Snapshot Record**: Capture all internal wire values. Detected faults at the injected location and forward: {detected_faults}.""",
"""1. **Fault Sensitivity Analysis**: Determine the impact of {fault_model_short} at node {fault_net} in module {module_name}.
2. **Path Selection**: Identify a logic path from {fault_net} to {primary_observation_nets}. Check for reconvergent fan-out that might mask the {fault_model_long} fault.
3. **Excitation Path**: Drive {fault_net} to {excitation_value} by setting {primary_controlling_nets}.
4. **Side-Input Conditioning**: Set all side-inputs of gates along the propagation path {propagation_gates} to non-controlling values: {non_controlling_nets}. Use {backtrack_gates} for backward justification.
5. **Conflict Detection**: Verify that no internal nodes are being driven to conflicting logic values by the chosen PI assignments.
6. **Signature Comparison**: Define the Good Machine expected output: {expected_output}. The Faulty Machine will differ at {primary_observation_nets}.
7. **Data Snapshot**: Generate the circuit simulation snapshot with input vector {input_vector}. Detected faults starting from the location where the fault occurred and onwards: {detected_faults}.""",
"""1. **Netlist Mapping**: Map the logic gates surrounding the fault {fault_net} in module {module_name}.
2. **Value Assignment**: Assign PIs via {primary_controlling_nets} to excite the {fault_model_long} fault (set {fault_net} to {excitation_value}).
3. **D-Frontier Drive**: Drive the fault effect through the sequence of gates {propagation_gates}. For each gate, justify the non-controlling values: {non_controlling_nets}.
4. **Logical Consistency**: Use backward implication through {backtrack_gates} to ensure all gate inputs are justified by the Primary Inputs.
5. **Detection Condition**: The {fault_model_short} fault is detected if at least one Primary Output in {primary_observation_nets} carries the fault signal.
6. **Vector Confirmation**: Input vector {input_vector} is validated by comparing the expected output {expected_output}.
7. **Final Snapshot**: Capture the logic state of the entire netlist. Detected faults from the injection point and onwards: {detected_faults}."""
]

_answer_template = ["""INPUT_VECTOR: "{input_vector}"
EXPECTED_OUTPUT: "{expected_output}"
DETECTED_FAULTS: "{detected_faults}" by propagating the fault forward.
"""]


chat_template = """{%- if tools %}
    {{- '<|im_start|>system\n' }}
    {%- if messages[0].role == 'system' %}
        {%- if messages[0].content is string %}
            {{- messages[0].content }}
        {%- else %}
            {%- for content in messages[0].content %}
                {%- if 'text' in content %}
                    {{- content.text }}
                {%- endif %}
            {%- endfor %}
        {%- endif %}
        {{- '\n\n' }}
    {%- endif %}
    {{- "# Tools\n\nYou may call one or more functions to assist with the user query.\n\nYou are provided with function signatures within <tools></tools> XML tags:\n<tools>" }}
    {%- for tool in tools %}
        {{- "\n" }}
        {{- tool | tojson }}
    {%- endfor %}
    {{- "\n</tools>\n\nFor each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call><|im_end|>\n" }}
{%- else %}
    {%- if messages[0].role == 'system' %}
        {{- '<|im_start|>system\n' }}
        {%- if messages[0].content is string %}
            {{- messages[0].content }}
        {%- else %}
            {%- for content in messages[0].content %}
                {%- if 'text' in content %}
                    {{- content.text }}
                {%- endif %}
            {%- endfor %}
        {%- endif %}
        {{- '<|im_end|>\n' }}
    {%- endif %}
{%- endif %}
{%- set image_count = namespace(value=0) %}
{%- set video_count = namespace(value=0) %}
{%- for message in messages %}
    {%- if message.role == "user" %}
        {{- '<|im_start|>' + message.role + '\n' }}
        {%- if message.content is string %}
            {{- message.content }}
        {%- else %}
            {%- for content in message.content %}
                {%- if content.type == 'image' or 'image' in content or 'image_url' in content %}
                    {%- set image_count.value = image_count.value + 1 %}
                    {%- if add_vision_id %}Picture {{ image_count.value }}: {% endif -%}
                    <|vision_start|><|image_pad|><|vision_end|>
                {%- elif content.type == 'video' or 'video' in content %}
                    {%- set video_count.value = video_count.value + 1 %}
                    {%- if add_vision_id %}Video {{ video_count.value }}: {% endif -%}
                    <|vision_start|><|video_pad|><|vision_end|>
                {%- elif 'text' in content %}
                    {{- content.text }}
                {%- endif %}
            {%- endfor %}
        {%- endif %}
        {{- '<|im_end|>\n' }}
    {%- elif message.role == "assistant" %}
        {{- '<|im_start|>' + message.role + '\n' }}
        {%- if message.content is string %}
            {{- message.content }}
        {%- else %}
            {%- for content_item in message.content %}
                {%- if 'text' in content_item %}
                    {{- content_item.text }}
                {%- endif %}
            {%- endfor %}
        {%- endif %}
        {%- if message.tool_calls %}
            {%- for tool_call in message.tool_calls %}
                {%- if (loop.first and message.content) or (not loop.first) %}
                    {{- '\n' }}
                {%- endif %}
                {%- if tool_call.function %}
                    {%- set tool_call = tool_call.function %}
                {%- endif %}
                {{- '<tool_call>\n{"name": "' }}
                {{- tool_call.name }}
                {{- '", "arguments": ' }}
                {%- if tool_call.arguments is string %}
                    {{- tool_call.arguments }}
                {%- else %}
                    {{- tool_call.arguments | tojson }}
                {%- endif %}
                {{- '}\n</tool_call>' }}
            {%- endfor %}
        {%- endif %}
        {{- '<|im_end|>\n' }}
    {%- elif message.role == "tool" %}
        {%- if loop.first or (messages[loop.index0 - 1].role != "tool") %}
            {{- '<|im_start|>user' }}
        {%- endif %}
        {{- '\n<tool_response>\n' }}
        {%- if message.content is string %}
            {{- message.content }}
        {%- else %}
            {%- for content in message.content %}
                {%- if content.type == 'image' or 'image' in content or 'image_url' in content %}
                    {%- set image_count.value = image_count.value + 1 %}
                    {%- if add_vision_id %}Picture {{ image_count.value }}: {% endif -%}
                    <|vision_start|><|image_pad|><|vision_end|>
                {%- elif content.type == 'video' or 'video' in content %}
                    {%- set video_count.value = video_count.value + 1 %}
                    {%- if add_vision_id %}Video {{ video_count.value }}: {% endif -%}
                    <|vision_start|><|video_pad|><|vision_end|>
                {%- elif 'text' in content %}
                    {{- content.text }}
                {%- endif %}
            {%- endfor %}
        {%- endif %}
        {{- '\n</tool_response>' }}
        {%- if loop.last or (messages[loop.index0 + 1].role != "tool") %}
            {{- '<|im_end|>\n' }}
        {%- endif %}
    {%- endif %}
{%- endfor %}
{%- if add_generation_prompt %}
    {{- '<|im_start|>assistant\n' }}
{%- endif %}
"""


atpg_thinking_process_template = """Generating Test Vectors

I am now focused on generating an ATPG test vector specifically targeting a {fault_model_short} fault on the {faulty_net} input of a {module_name} Verilog module. The ATPG tool's role is confirmed. I am now working on the specific details of vector generation.

Pinpointing Fault Effects

I'm deep into fault analysis for the "{fault_model_short} {faulty_net}" fault. My focus is now on exciting the fault to differentiate between good and faulty circuits, specifically targeting how {faulty_net} impacts the outputs. I'm carefully analyzing the standard cells involved, especially the {connected_gates_description}, to determine the necessary input conditions to propagate the fault effect to observable outputs like {observable_outputs_list}.

Refining Path Propagation

I have a concrete plan to detect the {fault_model_long} fault on the {faulty_net} input. The test vector needs {faulty_net} = {excitation_value} to drive the node. Analyzing the paths, especially through the {primary_propagation_gate} to {primary_observation_net}, reveals the key conditions for fault propagation. Setting {backtrack_logic} allows a clear distinction between the good and faulty circuit at the {primary_observation_net} output. This configuration ensures the fault is detectable at the output.

Evaluating Fault Propagation

I've determined I can propagate the {fault_model_long} fault on {faulty_net} to {secondary_observation_net} by {secondary_path_logic}. This configuration sets the inputs for the {secondary_observation_net} calculation, which allows the effect of a faulty circuit to differ from a functioning circuit. Analyzing the {remaining_outputs} output is next, since this analysis depends on the prior.

Synthesizing Test Vector

I've formulated the test vector. I've pinpointed the fault as {fault_model_long} on {faulty_net}. The excitation is achieved with {faulty_net} = {excitation_value}. I propagated the fault through {primary_propagation_gate} by setting {side_input_settings}, leading to a fault indication at {primary_observation_net}. This is one valid test vector generated, along with an explanation.

Analyzing Multiple Scenarios

I'm now expanding my analysis to include the scenario where {alternative_input_scenario}. In this scenario, with a good circuit, the {primary_observation_net} will be {snapshot_good_machine_val1} and {secondary_observation_net} will also be {snapshot_good_machine_val2}. However, a faulty circuit will cause the {secondary_observation_net} to become {snapshot_bad_machine_val}. I'm focusing on these various circuit output behaviors now.

Concluding Vector Design

I've decided on {final_vector_assignment} as the key test vector. I also am not adding image tags. It feels like the current explanation is better, without diagrams. My summary is complete.

Refining Fault Vectors

I'm now refining the fault vectors. I've confirmed that a {fault_model_long} fault on {faulty_net} is detectable with {faulty_net} = {excitation_value}. I will proceed to use this. I've evaluated that {final_justification_logic}, generating vector 1, and I've verified that {verification_logic}, generating vector 2.

"""
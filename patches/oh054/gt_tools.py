from litellm import ChatCompletionToolParam, ChatCompletionToolParamFunctionChunk

GtQueryTool = ChatCompletionToolParam(
    type='function',
    function=ChatCompletionToolParamFunctionChunk(
        name='gt_query',
        description=(
            'Look up callers, callees, tests, contracts, and signatures of a function/class/method '
            'using the indexed call graph. PREFER THIS over grep -r, find, or rg for any '
            '"who calls X" / "what tests cover X" / "what does X return" question — it is '
            'deterministic and complete across the whole repo, while grep misses dynamic dispatch. '
            'Output is tagged [VERIFIED] for import-resolved edges, [POSSIBLE] for name-match. '
            'Budget: 2 calls per task.'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'symbol': {
                    'type': 'string',
                    'description': 'Symbol to look up. Plain (parse_date), qualified (MyClass.parse_date), or any-case.',
                },
            },
            'required': ['symbol'],
        },
    ),
)

GtValidateTool = ChatCompletionToolParam(
    type='function',
    function=ChatCompletionToolParamFunctionChunk(
        name='gt_validate',
        description=(
            'Run BEFORE submitting. Structural sanity check on a file: (1) HALLUCINATED-IMPORT — '
            'flags imports that don\'t resolve to any graph node; (2) CALLER-BLIND-EDIT — flags '
            'symbols with 3+ callers whose signature changed; (3) CONTRACT-BREAK — flags '
            'signature/base-class changes. Catches bugs the test suite would catch. '
            'Budget: 3 calls per task.'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'file_path': {
                    'type': 'string',
                    'description': 'Repository-relative path to the edited file.',
                },
            },
            'required': ['file_path'],
        },
    ),
)

from unittest.mock import MagicMock

from snovault.elasticsearch.manage_mappings import ManageMappingsProps
from snovault.elasticsearch.manage_mappings import delete_old_indices
from snovault.elasticsearch.manage_mappings import make_summary


RESOURCES = 'resources'

CURRENT = {
    'snowflake': 'snowflake_1111',
    'lab': 'lab_2222',
}


def make_opensearch_client(aliased_indices, document_counts):
    # aliased_indices mirrors indices.get_alias('resources'):
    # {index_name: {'aliases': {alias: {}, ...}}}
    client = MagicMock()
    client.indices.get_alias.return_value = {
        index: {'aliases': {alias: {} for alias in aliases}}
        for index, aliases in aliased_indices.items()
    }
    client.count.side_effect = lambda index: {'count': document_counts[index]}
    return client


def make_props(opensearch_client, type_alias_to_current_index_name=CURRENT):
    return ManageMappingsProps(
        app=None,
        opensearch_client=opensearch_client,
        type_alias_to_current_index_name=type_alias_to_current_index_name,
        mappings={},
        all_resources_alias=RESOURCES,
        should_reindex='never',
        summary=make_summary(),
    )


def deleted_indices(client):
    return [
        call.kwargs['index']
        for call in client.indices.delete.call_args_list
    ]


def test_manage_mappings_make_summary_keys():
    assert sorted(make_summary()) == [
        'cleaned_up',
        'created',
        'deleted',
        'deleted_removed_types',
        'reindexed',
        'skipped_no_type_alias',
        'skipped_not_empty',
    ]


def test_manage_mappings_delete_old_indices_keeps_current_index():
    client = make_opensearch_client(
        {'snowflake_1111': ['snowflake', RESOURCES]},
        {'snowflake_1111': 10},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == []
    assert props.summary == make_summary()


def test_manage_mappings_delete_old_indices_skips_old_index_of_registered_type_with_documents():
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'snowflake_0000': ['snowflake', RESOURCES],
        },
        {'snowflake_1111': 10, 'snowflake_0000': 3},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == []
    assert props.summary['skipped_not_empty'] == ['snowflake_0000']
    assert props.summary['deleted'] == []
    assert props.summary['deleted_removed_types'] == []


def test_manage_mappings_delete_old_indices_deletes_empty_old_index_of_registered_type():
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'snowflake_0000': ['snowflake', RESOURCES],
        },
        {'snowflake_1111': 10, 'snowflake_0000': 0},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == ['snowflake_0000']
    assert props.summary['deleted'] == ['snowflake_0000']
    assert props.summary['skipped_not_empty'] == []
    assert props.summary['deleted_removed_types'] == []


def test_manage_mappings_delete_old_indices_deletes_index_of_unregistered_type_with_documents():
    # A type removed from the app: no reindex will ever drain its index.
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'treatment_8358168e': ['treatment', RESOURCES],
        },
        {'snowflake_1111': 10, 'treatment_8358168e': 4},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == ['treatment_8358168e']
    assert props.summary['deleted_removed_types'] == ['treatment_8358168e']
    assert props.summary['deleted'] == []
    assert props.summary['skipped_not_empty'] == []
    # The document count does not matter for a removed type.
    client.count.assert_not_called()


def test_manage_mappings_delete_old_indices_keeps_index_with_only_resources_alias():
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'mystery_index': [RESOURCES],
        },
        {'snowflake_1111': 10, 'mystery_index': 7},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == []
    assert props.summary['skipped_no_type_alias'] == ['mystery_index']
    assert props.summary['deleted_removed_types'] == []
    client.count.assert_not_called()


def test_manage_mappings_delete_old_indices_treats_mixed_aliases_as_registered_type():
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'odd_0000': ['snowflake', 'obsolete', RESOURCES],
        },
        {'snowflake_1111': 10, 'odd_0000': 2},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == []
    assert props.summary['skipped_not_empty'] == ['odd_0000']
    assert props.summary['deleted_removed_types'] == []

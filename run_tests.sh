#!/bin/bash
set -e

echo "=================================="
echo "ContextDB Tests"
echo "=================================="

if [ ! -d "contextdb" ]; then
    echo "Run from project root"
    exit 1
fi

TEST_TYPE=${1:-"all"}

run_test() {
    echo "[Running] $2..."
    if python "tests/$1"; then
        echo "[OK] $2"
    else
        echo "[FAIL] $2"
        return 1
    fi
}

case $TEST_TYPE in
    all)
        run_test "test_treedb.py" "TreeDB" || true
        run_test "test_config.py" "Config" || true
        run_test "test_pageindex.py" "PageIndex" || true
        run_test "test_retriever.py" "Retriever" || true
        ;;
    quick)
        run_test "test_treedb.py" "TreeDB"
        ;;
    ret)
        run_test "test_retriever.py" "Retriever"
        ;;
    *)
        echo "Usage: $0 [all|quick|ret]"
        exit 1
        ;;
esac

echo "=================================="
echo "Done"
echo "=================================="

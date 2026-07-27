"""按 issue 号命名的一次性复现/回归测试(regression 包)。

住这里的规矩:为某个编号 bug 写的复现脚本,修复后留作回归防线。
注意:本包文件在 test/ 下一层,找仓库根要三级 parent(Path(__file__).resolve().parent.parent.parent)。
"""

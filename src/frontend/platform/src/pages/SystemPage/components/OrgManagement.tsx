import { bsConfirm } from "@/components/bs-ui/alertDialog/useConfirm";
import { Button } from "@/components/bs-ui/button";
import { Input } from "@/components/bs-ui/input";
import { useToast } from "@/components/bs-ui/toast/use-toast";
import { captureAndAlertRequestErrorHoc } from "@/controllers/request";
import { createOrgNodeApi, deleteOrgNodeApi, getOrgNodeTreeApi, updateOrgNodeApi } from "@/controllers/API/user";
import {
    Building2,
    ChevronDown,
    ChevronRight,
    Pencil,
    Plus,
    Trash2,
    X,
    Check,
    Loader2
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

// ──────────────────── types ────────────────────

interface OrgNode {
    id: number;
    name: string;
    description?: string;
    level: number;
    parent_id: number | null;
    children: OrgNode[];
}

// ──────────────────── 行内输入框（新增/编辑共用）────────────────────

function InlineInput({
    defaultValue = '',
    placeholder = '请输入机构名称',
    onConfirm,
    onCancel,
    autoFocus = true,
}: {
    defaultValue?: string;
    placeholder?: string;
    onConfirm: (value: string) => Promise<void>;
    onCancel: () => void;
    autoFocus?: boolean;
}) {
    const [value, setValue] = useState(defaultValue);
    const [loading, setLoading] = useState(false);
    const inputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        if (autoFocus && inputRef.current) {
            inputRef.current.focus();
            inputRef.current.select();
        }
    }, []);

    const handleConfirm = async () => {
        const trimmed = value.trim();
        if (!trimmed) return;
        setLoading(true);
        try {
            await onConfirm(trimmed);
        } finally {
            setLoading(false);
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter') handleConfirm();
        if (e.key === 'Escape') onCancel();
    };

    return (
        <div className="flex items-center gap-1 flex-1">
            <Input
                ref={inputRef}
                value={value}
                onChange={e => setValue(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={placeholder}
                className="h-7 text-sm flex-1 py-0"
            />
            <Button
                size="sm"
                variant="ghost"
                className="h-7 w-7 p-0 text-green-600 hover:bg-green-50"
                onClick={handleConfirm}
                disabled={loading || !value.trim()}
            >
                {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
            </Button>
            <Button
                size="sm"
                variant="ghost"
                className="h-7 w-7 p-0 text-gray-400 hover:bg-gray-100"
                onClick={onCancel}
            >
                <X className="h-3.5 w-3.5" />
            </Button>
        </div>
    );
}

// ──────────────────── 单个节点行 ────────────────────

function OrgNodeRow({
    node,
    onCreated,
    onUpdated,
    onDeleted,
}: {
    node: OrgNode;
    onCreated: (parent_id: number, newNode: OrgNode) => void;
    onUpdated: (id: number, name: string) => void;
    onDeleted: (id: number) => void;
}) {
    const { message } = useToast();
    const [expanded, setExpanded] = useState(true);
    const [editMode, setEditMode] = useState(false);
    const [addingChild, setAddingChild] = useState(false);
    const MAX_LEVEL = 2; // LEVEL_ORG_3 = 2, level 从 0 计数

    const handleRename = async (newName: string) => {
        await captureAndAlertRequestErrorHoc(
            updateOrgNodeApi(node.id, { name: newName }).then(res => {
                onUpdated(node.id, newName);
                setEditMode(false);
                message({ variant: 'success', description: '修改成功' });
            })
        );
    };

    const handleAddChild = async (name: string) => {
        await captureAndAlertRequestErrorHoc(
            createOrgNodeApi({ name, parent_id: node.id }).then(res => {
                const nodeData = res?.data !== undefined ? res.data : res;
                onCreated(node.id, nodeData);
                setAddingChild(false);
                setExpanded(true);
                message({ variant: 'success', description: '子节点创建成功' });
            })
        );
    };

    const handleDelete = () => {
        bsConfirm({
            title: '确认删除',
            desc: `确定删除机构节点「${node.name}」？若有知识空间引用此节点，将自动解除绑定。`,
            onOk(next) {
                captureAndAlertRequestErrorHoc(
                    deleteOrgNodeApi(node.id).then(res => {
                        const data = res?.data !== undefined ? res.data : res;
                        onDeleted(node.id);
                        message({
                            variant: 'success',
                            description: data?.unlinked_spaces > 0
                                ? `删除成功，已解除 ${data.unlinked_spaces} 个知识空间的机构绑定`
                                : '删除成功',
                        });
                    })
                );
                next();
            },
        });
    };

    const indentPx = node.level * 20;

    return (
        <div>
            <div
                className="group flex items-center gap-1.5 py-1.5 px-2 rounded-md hover:bg-gray-50 transition-colors"
                style={{ paddingLeft: `${8 + indentPx}px` }}
            >
                {/* 展开/折叠 */}
                <button
                    className="shrink-0 text-gray-400 hover:text-gray-600 w-5 h-5 flex items-center justify-center"
                    onClick={() => setExpanded(v => !v)}
                >
                    {node.children.length > 0
                        ? expanded
                            ? <ChevronDown className="h-3.5 w-3.5" />
                            : <ChevronRight className="h-3.5 w-3.5" />
                        : <span className="w-3.5" />
                    }
                </button>

                <Building2 className="h-4 w-4 shrink-0 text-blue-500" />

                {/* 节点名称 or 编辑框 */}
                {editMode ? (
                    <InlineInput
                        defaultValue={node.name}
                        onConfirm={handleRename}
                        onCancel={() => setEditMode(false)}
                    />
                ) : (
                    <span className="flex-1 text-sm font-medium text-gray-800 truncate">
                        {node.name}
                    </span>
                )}

                {/* 操作按钮（hover 时显示） */}
                {!editMode && (
                    <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                        {/* 编辑 */}
                        <Button
                            size="sm"
                            variant="ghost"
                            className="h-6 w-6 p-0 text-gray-400 hover:text-blue-500 hover:bg-blue-50"
                            onClick={() => setEditMode(true)}
                            title="重命名"
                        >
                            <Pencil className="h-3 w-3" />
                        </Button>
                        {/* 新增子节点（最多 3 级） */}
                        {node.level < MAX_LEVEL && (
                            <Button
                                size="sm"
                                variant="ghost"
                                className="h-6 w-6 p-0 text-gray-400 hover:text-green-600 hover:bg-green-50"
                                onClick={() => { setAddingChild(true); setExpanded(true); }}
                                title="新增子机构"
                            >
                                <Plus className="h-3 w-3" />
                            </Button>
                        )}
                        {/* 删除 */}
                        <Button
                            size="sm"
                            variant="ghost"
                            className="h-6 w-6 p-0 text-gray-400 hover:text-red-500 hover:bg-red-50"
                            onClick={handleDelete}
                            title="删除"
                        >
                            <Trash2 className="h-3 w-3" />
                        </Button>
                    </div>
                )}
            </div>

            {/* 子节点 */}
            {expanded && (
                <div>
                    {node.children.map(child => (
                        <OrgNodeRow
                            key={child.id}
                            node={child}
                            onCreated={onCreated}
                            onUpdated={onUpdated}
                            onDeleted={onDeleted}
                        />
                    ))}
                    {/* 行内新增子节点输入框 */}
                    {addingChild && (
                        <div
                            className="flex items-center gap-1.5 py-1.5 px-2"
                            style={{ paddingLeft: `${8 + (node.level + 1) * 20}px` }}
                        >
                            <Building2 className="h-4 w-4 shrink-0 text-gray-300" />
                            <InlineInput
                                placeholder="输入子机构名称，回车确认"
                                onConfirm={handleAddChild}
                                onCancel={() => setAddingChild(false)}
                            />
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

// ──────────────────── 主组件 ────────────────────

export default function OrgManagement() {
    const { t } = useTranslation();
    const { message } = useToast();
    const [nodes, setNodes] = useState<OrgNode[]>([]);
    const [loading, setLoading] = useState(true);
    const [addingRoot, setAddingRoot] = useState(false);

    // 加载机构树
    const loadTree = async () => {
        setLoading(true);
        try {
            const res = await getOrgNodeTreeApi();
            const list = Array.isArray(res) ? res : (res?.data || []);
            setNodes(list);
        } catch (e) {
            message({ variant: 'error', description: '加载机构树失败' });
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => { loadTree(); }, []);

    // ── 本地状态更新，避免整树重新请求 ──

    const insertNode = (tree: OrgNode[], parentId: number | null, newNode: OrgNode): OrgNode[] => {
        if (parentId === null) return [...tree, newNode];
        return tree.map(n => {
            if (n.id === parentId) return { ...n, children: [...n.children, newNode] };
            return { ...n, children: insertNode(n.children, parentId, newNode) };
        });
    };

    const updateNodeName = (tree: OrgNode[], id: number, name: string): OrgNode[] =>
        tree.map(n => {
            if (n.id === id) return { ...n, name };
            return { ...n, children: updateNodeName(n.children, id, name) };
        });

    const removeNode = (tree: OrgNode[], id: number): OrgNode[] =>
        tree.filter(n => n.id !== id).map(n => ({ ...n, children: removeNode(n.children, id) }));

    // 新增根节点
    const handleAddRoot = async (name: string) => {
        await captureAndAlertRequestErrorHoc(
            createOrgNodeApi({ name, parent_id: null }).then(res => {
                const nodeData = res?.data !== undefined ? res.data : res;
                setNodes(prev => insertNode(prev, null, nodeData));
                setAddingRoot(false);
                message({ variant: 'success', description: '一级机构创建成功' });
            })
        );
    };

    return (
        <div className="relative h-[calc(100vh-130px)] flex flex-col">
            {/* 顶部操作栏 */}
            <div className="flex items-center justify-between py-3 shrink-0">
                <p className="text-sm text-gray-500">
                    共 <span className="font-semibold text-gray-700">{nodes.length}</span> 个一级机构
                </p>
                <Button
                    size="sm"
                    className="h-8 gap-1.5"
                    onClick={() => setAddingRoot(true)}
                    disabled={addingRoot}
                >
                    <Plus className="h-4 w-4" />
                    新增一级机构
                </Button>
            </div>

            {/* 机构树区域 */}
            <div className="flex-1 overflow-y-auto border rounded-lg bg-white">
                {loading ? (
                    <div className="flex items-center justify-center h-32 text-gray-400">
                        <Loader2 className="h-5 w-5 animate-spin mr-2" />
                        加载中...
                    </div>
                ) : (
                    <div className="p-2">
                        {/* 新增根节点输入框 */}
                        {addingRoot && (
                            <div className="flex items-center gap-1.5 py-1.5 px-2 mb-1 bg-blue-50 rounded-md border border-blue-100">
                                <Building2 className="h-4 w-4 shrink-0 text-blue-400" />
                                <InlineInput
                                    placeholder="输入一级机构名称，回车确认"
                                    onConfirm={handleAddRoot}
                                    onCancel={() => setAddingRoot(false)}
                                />
                            </div>
                        )}

                        {nodes.length === 0 && !addingRoot ? (
                            <div className="flex flex-col items-center justify-center py-16 text-gray-400 gap-3">
                                <Building2 className="h-12 w-12 opacity-30" />
                                <p className="text-sm">暂无机构数据</p>
                                <p className="text-xs">点击右上角「新增一级机构」开始建立组织架构</p>
                            </div>
                        ) : (
                            nodes.map(node => (
                                <OrgNodeRow
                                    key={node.id}
                                    node={node}
                                    onCreated={(parentId, newNode) =>
                                        setNodes(prev => insertNode(prev, parentId, newNode))
                                    }
                                    onUpdated={(id, name) =>
                                        setNodes(prev => updateNodeName(prev, id, name))
                                    }
                                    onDeleted={(id) =>
                                        setNodes(prev => removeNode(prev, id))
                                    }
                                />
                            ))
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}

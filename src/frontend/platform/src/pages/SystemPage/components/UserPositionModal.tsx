import { Button } from "@/components/bs-ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/bs-ui/command"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/bs-ui/dialog"
import { Input } from "@/components/bs-ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/bs-ui/popover"
import { useToast } from "@/components/bs-ui/toast/use-toast"
import { getOrgNodeTreeApi, getUserPositionsApi, setUserPositionsApi } from "@/controllers/API/user"
import { captureAndAlertRequestErrorHoc } from "@/controllers/request"
import { Building2, Check, ChevronsUpDown, Plus, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

/**
 * 展平组织架构树，带有缩进深度 depth
 */
function flattenTree(nodes: any[], depth = 0): any[] {
    let result: any[] = [];
    for (const node of nodes) {
        result.push({ ...node, depth });
        if (node.children && node.children.length > 0) {
            result = result.concat(flattenTree(node.children, depth + 1));
        }
    }
    return result;
}

export default function UserPositionModal({ user, onClose, onChange }) {
    const { t } = useTranslation();
    const { message } = useToast();

    // 组织机构树数据
    const [orgNodes, setOrgNodes] = useState<any[]>([]);
    const [orgLoading, setOrgLoading] = useState(false);

    // 当前用户的职务列表
    const [positions, setPositions] = useState<{ kb_node_id: number; kb_node_name?: string; position_name: string }[]>([]);
    const [loading, setLoading] = useState(false);

    // 新增项状态
    const [openCombobox, setOpenCombobox] = useState(false);
    const [selectedNodeId, setSelectedNodeId] = useState<number | null>(null);
    const [newPositionName, setNewPositionName] = useState("");

    // 弹窗打开时加载数据
    useEffect(() => {
        if (!user) return;

        let cancelled = false;

        const loadOrgTree = async () => {
            setOrgLoading(true);
            try {
                const res = await getOrgNodeTreeApi();
                if (!cancelled) {
                    const rawData = Array.isArray(res) ? res : (res?.data || []);
                    const flatNodes = flattenTree(rawData);
                    setOrgNodes(flatNodes);
                }
            } catch (err) {
                console.error("Failed to load org tree", err);
            } finally {
                if (!cancelled) setOrgLoading(false);
            }
        };

        const loadPositions = async () => {
            setLoading(true);
            try {
                const res = await getUserPositionsApi(user.user_id);
                if (!cancelled) {
                    setPositions(Array.isArray(res) ? res : (res?.data || []));
                }
            } catch (err) {
                console.error("Failed to load positions", err);
            } finally {
                if (!cancelled) setLoading(false);
            }
        };

        loadOrgTree();
        loadPositions();

        return () => { cancelled = true; };
    }, [user]);

    // 重置状态
    const handleClose = () => {
        setSelectedNodeId(null);
        setNewPositionName("");
        setPositions([]);
        onClose();
    };

    // 添加新职务（暂存）
    const handleAddPosition = () => {
        if (!selectedNodeId) {
            return message({ title: t('prompt'), variant: 'warning', description: t('system.selectGroup', '请选择组织节点') });
        }

        // 防重校验：同一组织节点下不能有多个职务
        if (positions.some(p => p.kb_node_id === selectedNodeId)) {
            return message({ title: t('prompt'), variant: 'warning', description: '该用户已拥有此组织的职务，请直接在列表中修改' });
        }

        const node = orgNodes.find(n => n.id === selectedNodeId);

        setPositions([
            ...positions,
            {
                kb_node_id: selectedNodeId,
                kb_node_name: node ? node.name : String(selectedNodeId),
                position_name: newPositionName.trim()
            }
        ]);

        // 重置新建区
        setSelectedNodeId(null);
        setNewPositionName("");
    };

    // 修改职务名称 (行内编辑)
    const handleUpdatePositionName = (index: number, newName: string) => {
        setPositions(prev => {
            const next = [...prev];
            next[index].position_name = newName;
            return next;
        });
    };

    // 移除职务
    const handleRemovePosition = (index: number) => {
        setPositions(prev => prev.filter((_, i) => i !== index));
    };

    // 保存提交至后端
    const handleSave = () => {
        const payload = positions.map(p => ({
            kb_node_id: p.kb_node_id,
            position_name: p.position_name.trim()
        }));

        // 【终极排障防线】：如果此时的 payload 是空的，说明列表里根本没有待分配的职务，直接拦截！
        if (payload.length === 0) {
            return message({
                title: '拦截报警',
                variant: 'error',
                description: '检测到您即将向服务器发送【空列表】！这会清空该用户的所有职务。如果这不是您的本意，说明您刚才点击 + 号并未成功将职务加入列表。'
            });
        }

        captureAndAlertRequestErrorHoc(
            setUserPositionsApi(user.user_id, payload).then(() => {
                message({ title: t('prompt'), variant: 'success', description: t('saveSuccess', '保存成功') });
                onChange();
                handleClose();
            })
        );
    };

    const selectedNode = orgNodes.find(n => n.id === selectedNodeId);

    return (
        <Dialog open={!!user} onOpenChange={(open) => !open && handleClose()}>
            <DialogContent className="sm:max-w-[700px]">
                <DialogHeader>
                    <DialogTitle>{t('system.positionConfig', '职务配置')} - {user?.user_name}</DialogTitle>
                </DialogHeader>

                <div className="py-4 flex flex-col gap-4">
                    {/* 新增区域 */}
                    <div className="flex gap-2 items-end bg-gray-50 p-4 rounded-md border border-gray-100">
                        <div className="flex-1 space-y-2">
                            <label className="text-sm font-medium">选择组织节点</label>
                            <Popover open={openCombobox} onOpenChange={setOpenCombobox}>
                                <PopoverTrigger asChild>
                                    <Button
                                        variant="outline"
                                        role="combobox"
                                        aria-expanded={openCombobox}
                                        className="w-full justify-between font-normal bg-white"
                                    >
                                        <div className="flex items-center gap-2 overflow-hidden">
                                            <Building2 className="h-4 w-4 shrink-0 text-gray-500" />
                                            <span className="truncate">
                                                {selectedNode ? selectedNode.name : "搜索组织节点..."}
                                            </span>
                                        </div>
                                        <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                                    </Button>
                                </PopoverTrigger>
                                <PopoverContent className="w-[300px] p-0" align="start">
                                    <Command>
                                        <CommandInput placeholder="输入名称搜索组织..." />
                                        <CommandEmpty>未找到该组织</CommandEmpty>
                                        <CommandList>
                                            <CommandGroup>
                                                {orgNodes.map((node) => (
                                                    <CommandItem
                                                        key={node.id}
                                                        value={node.name}
                                                        onSelect={() => {
                                                            setSelectedNodeId(node.id);
                                                            setOpenCombobox(false);
                                                        }}
                                                    >
                                                        <Check
                                                            className={`mr-2 h-4 w-4 ${selectedNodeId === node.id ? "opacity-100" : "opacity-0"}`}
                                                        />
                                                        <div
                                                            className="flex-1 truncate"
                                                            style={{
                                                                paddingLeft: `${node.depth * 12}px`
                                                            }}
                                                        >
                                                            {node.depth > 0 && <span className="text-gray-300 mr-1">└─</span>}
                                                            {node.name}
                                                        </div>
                                                    </CommandItem>
                                                ))}
                                            </CommandGroup>
                                        </CommandList>
                                    </Command>
                                </PopoverContent>
                            </Popover>
                        </div>

                        <div className="w-[200px] space-y-2">
                            <label className="text-sm font-medium">职务名称</label>
                            <Input
                                placeholder="如: 院长、学生..."
                                value={newPositionName}
                                onChange={(e) => setNewPositionName(e.target.value)}
                                className="bg-white"
                            />
                        </div>

                        <Button onClick={handleAddPosition} className="shrink-0 h-10 w-10 p-0" variant="secondary">
                            <Plus className="h-5 w-5" />
                        </Button>
                    </div>

                    {/* 已有列表区域 */}
                    <div className="mt-2 space-y-2">
                        <h4 className="text-sm font-medium">已分配的职务 ({positions.length})</h4>
                        {loading ? (
                            <div className="text-sm text-gray-500 py-4 text-center">加载中...</div>
                        ) : positions.length === 0 ? (
                            <div className="text-sm text-gray-400 py-6 text-center border rounded-md border-dashed">暂未分配任何职务</div>
                        ) : (
                            <div className="max-h-[300px] overflow-y-auto pr-2 space-y-2">
                                {positions.map((pos, idx) => (
                                    <div key={idx} className="flex items-center gap-3 bg-white p-3 border rounded-md shadow-sm">
                                        <div className="flex-1 min-w-0">
                                            <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
                                                <Building2 className="h-4 w-4 text-gray-400 shrink-0" />
                                                <span className="truncate">{pos.kb_node_name || pos.kb_node_id}</span>
                                            </div>
                                        </div>
                                        <div className="w-[200px]">
                                            <Input
                                                value={pos.position_name}
                                                onChange={(e) => handleUpdatePositionName(idx, e.target.value)}
                                                placeholder="职务名称"
                                                className="h-8 text-sm"
                                            />
                                        </div>
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            className="text-red-500 hover:text-red-600 hover:bg-red-50 h-8 w-8 p-0 shrink-0"
                                            onClick={() => handleRemovePosition(idx)}
                                        >
                                            <Trash2 className="h-4 w-4" />
                                        </Button>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>

                <DialogFooter>
                    <Button variant="outline" onClick={handleClose} className="w-[120px]">{t('cancel')}</Button>
                    <Button onClick={handleSave} className="w-[120px]">{t('save')}</Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}

import { useState, useEffect, useRef } from "react";
import { X, Search, Trash2, Shield, User, Landmark, Users } from "lucide-react";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogFooter,
    Button,
} from "~/components/ui";
import { useToastContext } from "~/Providers";
import {
    PermissionRule,
    PermissionCandidate,
    getFilePermissionsApi,
    saveFilePermissionsApi,
    getPermissionCandidatesApi,
} from "~/api/knowledge";
import { useLocalize } from "~/hooks";

interface DocumentPermissionsDialogProps {
    isOpen: boolean;
    onClose: () => void;
    spaceId: string;
    fileId: string;
    fileName: string;
}

export function DocumentPermissionsDialog({
    isOpen,
    onClose,
    spaceId,
    fileId,
    fileName,
}: DocumentPermissionsDialogProps) {
    const localize = useLocalize();
    const { showToast } = useToastContext();

    const [isPrivate, setIsPrivate] = useState(false);
    const [rules, setRules] = useState<PermissionRule[]>([]);
    const [myPermission, setMyPermission] = useState<string | null>(null);

    const [searchKeyword, setSearchKeyword] = useState("");
    const [candidates, setCandidates] = useState<PermissionCandidate[]>([]);
    const [showCandidatesDropdown, setShowCandidatesDropdown] = useState(false);
    const [loading, setLoading] = useState(false);
    const dropdownRef = useRef<HTMLDivElement>(null);

    // Fetch initial permissions
    useEffect(() => {
        if (!isOpen || !spaceId || !fileId) return;
        setLoading(true);
        getFilePermissionsApi(spaceId, fileId)
            .then((res) => {
                setIsPrivate(res.is_private);
                setRules(res.rules);
                setMyPermission(res.my_permission);
            })
            .catch(() => {
                showToast({ message: "获取文档权限配置失败", status: "error" });
            })
            .finally(() => {
                setLoading(false);
            });
    }, [isOpen, spaceId, fileId]);

    // Handle candidates search
    useEffect(() => {
        if (!searchKeyword.trim()) {
            setCandidates([]);
            return;
        }
        const delayDebounce = setTimeout(() => {
            getPermissionCandidatesApi(spaceId, searchKeyword)
                .then((res) => {
                    // Filter out already added candidates
                    const existingKeys = new Set(rules.map((r) => `${r.subject_type}-${r.subject_id}`));
                    const filtered = res.filter((c) => !existingKeys.has(`${c.subject_type}-${c.subject_id}`));
                    setCandidates(filtered);
                })
                .catch(() => { });
        }, 300);

        return () => clearTimeout(delayDebounce);
    }, [searchKeyword, spaceId, rules]);

    // Handle clicking outside candidates dropdown to close it
    useEffect(() => {
        function handleClickOutside(event: MouseEvent) {
            if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
                setShowCandidatesDropdown(false);
            }
        }
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, []);

    // Add candidate to rules
    const handleAddRule = (candidate: PermissionCandidate) => {
        const newRule: PermissionRule = {
            subject_type: candidate.subject_type,
            subject_id: candidate.subject_id,
            subject_name: candidate.subject_name,
            permission_type: "read",
        };
        setRules((prev) => [...prev, newRule]);
        setSearchKeyword("");
        setShowCandidatesDropdown(false);
    };

    // Remove a rule
    const handleRemoveRule = (index: number) => {
        setRules((prev) => prev.filter((_, i) => i !== index));
    };

    // Update permission type for a rule
    const handleUpdateRulePerm = (index: number, permType: "read" | "write" | "admin") => {
        setRules((prev) => {
            const next = [...prev];
            next[index] = { ...next[index], permission_type: permType };
            return next;
        });
    };

    // Save permissions configuration
    const handleSave = async () => {
        setLoading(true);
        try {
            const cleanRules = rules.map(({ subject_type, subject_id, permission_type }) => ({
                subject_type,
                subject_id,
                permission_type,
            }));
            await saveFilePermissionsApi(spaceId, fileId, {
                is_private: isPrivate,
                rules: cleanRules,
            });
            showToast({ message: "文档权限配置保存成功", status: "success" });
            onClose();
        } catch {
            showToast({ message: "文档权限配置保存失败", status: "error" });
        } finally {
            setLoading(false);
        }
    };

    const getSubjectIcon = (type: string) => {
        switch (type) {
            case "all":
                return <Users className="w-4 h-4 text-blue-500 shrink-0" />;
            case "user":
                return <User className="w-4 h-4 text-emerald-500 shrink-0" />;
            case "org":
                return <Landmark className="w-4 h-4 text-amber-500 shrink-0" />;
            default:
                return null;
        }
    };

    return (
        <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
            <DialogContent className="gap-0 sm:max-w-[620px] w-[620px] p-0 bg-white border-none shadow-[0px_5px_22px_0px_rgba(61,68,110,0.2)] rounded-xl outline-none flex flex-col items-stretch [&>button]:hidden">
                <DialogHeader className="px-6 py-4 border-b border-[#ebecf0] flex flex-row items-center justify-between shrink-0">
                    <DialogTitle className="text-[16px] font-semibold text-[#1d2129] leading-[24px] flex items-center gap-2">
                        <Shield className="w-5 h-5 text-primary" />
                        <span>权限配置 - {fileName}</span>
                    </DialogTitle>
                    <button
                        className="text-[#86909c] hover:text-[#4e5969] transition-colors flex items-center justify-center w-6 h-6 rounded-md hover:bg-gray-100"
                        onClick={onClose}
                    >
                        <X className="w-4 h-4" />
                    </button>
                </DialogHeader>

                <div className="flex flex-col flex-1 gap-5 px-6 py-5 min-h-[380px] max-h-[500px] overflow-y-auto">
                    {/* Private Switch Row */}
                    <div className="flex items-center justify-between p-3.5 bg-gray-50 border border-gray-100 rounded-lg">
                        <div className="flex flex-col gap-0.5">
                            <span className="text-sm font-medium text-[#1d2129]">私有文档</span>
                            <span className="text-xs text-[#86909c]">
                                新上传文档默认为私有。开启后仅您本人及白名单成员可访问；关闭且无白名单时，按空间可见性（公开/审批/私有）对成员开放。
                            </span>
                        </div>
                        <label className="relative inline-flex items-center cursor-pointer select-none">
                            <input
                                type="checkbox"
                                checked={isPrivate}
                                onChange={(e) => setIsPrivate(e.target.checked)}
                                className="sr-only peer"
                            />
                            <div className="w-10 h-6 bg-gray-200 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary"></div>
                        </label>
                    </div>

                    {/* 白名单规则：可与私有开关组合使用，也可单独限制特定协作者 */}
                    <div className="flex flex-col flex-1 gap-4">
                        <div className="flex flex-col gap-2 relative" ref={dropdownRef}>
                            <span className="text-sm font-medium text-[#1d2129]">添加协作者或部门</span>
                            <div className="relative">
                                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                                <input
                                    type="text"
                                    value={searchKeyword}
                                    onChange={(e) => {
                                        setSearchKeyword(e.target.value);
                                        setShowCandidatesDropdown(true);
                                    }}
                                    onFocus={() => setShowCandidatesDropdown(true)}
                                    placeholder="输入用户名称或空间名称进行模糊搜索..."
                                    className="w-full pl-9 pr-4 py-2 border border-gray-200 rounded-lg text-sm bg-white placeholder-gray-400 focus:outline-none focus:border-primary transition-colors"
                                />
                            </div>

                            {/* Dropdown Results */}
                            {showCandidatesDropdown && searchKeyword.trim() && (
                                <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-gray-100 rounded-lg shadow-lg max-h-56 overflow-y-auto z-[99] py-1">
                                    {candidates.length === 0 ? (
                                        <div className="px-4 py-3 text-sm text-gray-400 text-center">未检索到匹配的候选对象</div>
                                    ) : (
                                        candidates.map((c) => (
                                            <div
                                                key={`${c.subject_type}-${c.subject_id}`}
                                                onClick={() => handleAddRule(c)}
                                                className="px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 cursor-pointer flex items-center gap-2.5 transition-colors"
                                            >
                                                {getSubjectIcon(c.subject_type)}
                                                <span className="font-medium flex-1">{c.subject_name}</span>
                                                <span className="text-xs text-gray-400">
                                                    {c.subject_type === "all" ? "所有人" : c.subject_type === "org" ? "知识空间/部门" : "用户"}
                                                </span>
                                            </div>
                                        ))
                                    )}
                                </div>
                            )}
                        </div>

                        {/* Rules list */}
                        <div className="flex flex-col gap-2.5">
                            <span className="text-sm font-medium text-[#1d2129]">已授权的协作者 ({rules.length})</span>
                            <div className="flex flex-col gap-2 max-h-[220px] overflow-y-auto border border-gray-100 rounded-lg p-2.5 bg-gray-50/50">
                                {rules.length === 0 ? (
                                    <div className="text-xs text-gray-400 text-center py-6">暂无任何白名单规则，请在上方搜索并添加</div>
                                ) : (
                                    rules.map((rule, idx) => (
                                        <div
                                            key={idx}
                                            className="flex items-center justify-between p-2 bg-white border border-gray-200/80 rounded-md shadow-sm transition-all"
                                        >
                                            <div className="flex items-center gap-2 min-w-0">
                                                {getSubjectIcon(rule.subject_type)}
                                                <span className="text-sm font-medium text-gray-700 truncate max-w-[240px]">
                                                    {rule.subject_name}
                                                </span>
                                            </div>

                                            <div className="flex items-center gap-3">
                                                <select
                                                    value={rule.permission_type}
                                                    onChange={(e) => handleUpdateRulePerm(idx, e.target.value as any)}
                                                    className="bg-transparent border-none text-xs font-medium text-[#165dff] focus:ring-0 cursor-pointer outline-none"
                                                >
                                                    <option value="read">只读 (read)</option>
                                                    <option value="write">编辑 (write)</option>
                                                    <option value="admin">管理 (admin)</option>
                                                </select>
                                                <button
                                                    onClick={() => handleRemoveRule(idx)}
                                                    className="text-gray-400 hover:text-red-500 transition-colors p-1"
                                                >
                                                    <Trash2 className="w-3.5 h-3.5" />
                                                </button>
                                            </div>
                                        </div>
                                    ))
                                )}
                            </div>
                        </div>
                    </div>
                </div>

                <DialogFooter className="flex justify-end gap-3 px-6 py-4 border-t border-[#ebecf0] sm:space-x-0 h-16 items-center shrink-0">
                    <Button variant="outline" className="h-8 px-4" onClick={onClose} disabled={loading}>
                        取消
                    </Button>
                    <Button variant="default" className="h-8 px-4" onClick={handleSave} disabled={loading}>
                        确定
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}

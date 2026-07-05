import React, { useState } from 'react';
import { DocxIcon, PptxIcon, XlsxIcon, TxtIcon } from '~/components/icons';
import { FileStatus } from '~/api/knowledge';

const iconSlotClass = 'size-[64px] shrink-0 object-contain';

const GENERIC_FILE_PLACEHOLDER = `${__APP_ENV__.BASE_URL}/assets/channel/notebook-one.svg`;

const iconMap: Record<string, React.ReactNode> = {
    doc: <DocxIcon className={iconSlotClass} />,
    docx: <DocxIcon className={iconSlotClass} />,
    ppt: <PptxIcon className={iconSlotClass} />,
    pptx: <PptxIcon className={iconSlotClass} />,
    xls: <XlsxIcon className={iconSlotClass} />,
    xlsx: <XlsxIcon className={iconSlotClass} />,
    txt: <TxtIcon className={iconSlotClass} />,
};

function FileTypeFallback({ extension }: { extension?: string }) {
    return (
        <div className="flex items-center justify-center w-full h-full">
            {iconMap[extension ?? ''] ?? (
                <img
                    src={GENERIC_FILE_PLACEHOLDER}
                    alt=""
                    className="size-[56px] object-contain opacity-80"
                />
            )}
        </div>
    );
}

function FileThumbnail({ file }: { file: { name?: string; thumbnail?: string; status?: FileStatus } }) {
    const [failed, setFailed] = useState(false);
    const extension = file.name?.split('.').pop()?.toLowerCase();

    if (!file.thumbnail || file.status !== FileStatus.SUCCESS || failed) {
        return <FileTypeFallback extension={extension} />;
    }

    return (
        <img
            src={file.thumbnail}
            alt={file.name}
            className="w-full h-full object-contain"
            onError={() => setFailed(true)}
        />
    );
}

const FileIconRenderer = ({ file, isFolder }: { file: any; isFolder: boolean }) => {
    if (isFolder) {
        return (
            <img
                src={`${__APP_ENV__.BASE_URL}/assets/channel/Folder.svg`}
                alt=""
                className={iconSlotClass}
            />
        );
    }

    return <FileThumbnail file={file} />;
};

export default FileIconRenderer;

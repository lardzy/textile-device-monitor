import {
  Alert,
  Checkbox,
  Input,
  message,
  Modal,
  Space,
  Spin,
  Typography,
} from 'antd';
import { useEffect, useState } from 'react';
import { areaApi } from '../../api/area';
import { getDefaultRenameName } from '../../utils/folderRename';
import { getAreaErrorMessage } from './areaUtils';

function AreaFolderCleanupModal({
  folder,
  onClose,
  onCompleted,
}) {
  const [cleanupPreview, setCleanupPreview] = useState(null);
  const [cleanupLoading, setCleanupLoading] = useState(false);
  const [cleanupExecuting, setCleanupExecuting] = useState(false);
  const [renameEnabled, setRenameEnabled] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');

  useEffect(() => {
    setCleanupPreview(null);
    setCleanupLoading(Boolean(folder?.folder_name));
    setRenameEnabled(false);
    setNewFolderName(getDefaultRenameName(folder?.folder_name));
  }, [folder]);

  useEffect(() => {
    if (!folder?.folder_name) return undefined;
    let active = true;
    setCleanupLoading(true);
    const timer = window.setTimeout(async () => {
      try {
        const payload = await areaApi.previewFolderCleanup(folder.folder_name, {
          rename_enabled: renameEnabled,
          new_folder_name: renameEnabled ? newFolderName.trim() : null,
        });
        if (active) setCleanupPreview(payload);
      } catch (error) {
        if (active) {
          setCleanupPreview(null);
          message.error(getAreaErrorMessage(error, '目录整理预检失败'));
        }
      } finally {
        if (active) setCleanupLoading(false);
      }
    }, 250);

    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [folder, newFolderName, renameEnabled]);

  const executeCleanup = async () => {
    if (!folder?.folder_name) return;
    if (renameEnabled && !newFolderName.trim()) {
      message.warning('请输入新的文件夹名称');
      return;
    }
    if (cleanupPreview?.rename_target_exists) {
      message.error('目标文件夹名称已存在');
      return;
    }

    setCleanupExecuting(true);
    try {
      const result = await areaApi.cleanupFolder(folder.folder_name, {
        rename_enabled: renameEnabled,
        new_folder_name: renameEnabled ? newFolderName.trim() : null,
      });
      message.success(`目录整理完成，已移动 ${Number(result?.moved || 0)} 个文件`);
      onClose();
      await onCompleted?.(result);
    } catch (error) {
      message.error(getAreaErrorMessage(error, '目录整理失败'));
    } finally {
      setCleanupExecuting(false);
    }
  };

  return (
    <Modal
      open={Boolean(folder)}
      title="整理采集目录"
      okText="确认整理"
      okButtonProps={{
        danger: true,
        loading: cleanupExecuting,
        disabled: cleanupLoading || !cleanupPreview || cleanupPreview.rename_target_exists,
      }}
      cancelButtonProps={{ disabled: cleanupExecuting }}
      cancelText="取消"
      closable={!cleanupExecuting}
      keyboard={!cleanupExecuting}
      maskClosable={!cleanupExecuting}
      onCancel={() => {
        if (!cleanupExecuting) onClose();
      }}
      onOk={executeCleanup}
    >
      <Alert
        type="warning"
        showIcon
        message="此操作会移动目录中的原始图片"
        description="非 *_i.jpg / *_i.jpeg 图片将移入同级 .recycle 目录，保留的采集图片不会被删除。"
      />
      <Spin spinning={cleanupLoading}>
        <div className="area-cleanup-summary">
          <Typography.Text>将移动</Typography.Text>
          <Typography.Title level={3}>{Number(cleanupPreview?.move_count || 0)}</Typography.Title>
          <Typography.Text type="secondary">个文件</Typography.Text>
          <Typography.Text>
            保留 {Number(cleanupPreview?.keep_count || 0)} 个采集文件
          </Typography.Text>
        </div>
      </Spin>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Checkbox checked={renameEnabled} onChange={(event) => setRenameEnabled(event.target.checked)}>
          整理后重命名文件夹
        </Checkbox>
        {renameEnabled ? (
          <Input
            status={cleanupPreview?.rename_target_exists ? 'error' : undefined}
            value={newFolderName}
            onChange={(event) => setNewFolderName(event.target.value)}
            placeholder="新的文件夹名称"
          />
        ) : null}
        {cleanupPreview?.rename_target_exists ? (
          <Typography.Text type="danger">目标文件夹已经存在</Typography.Text>
        ) : null}
      </Space>
    </Modal>
  );
}

export default AreaFolderCleanupModal;

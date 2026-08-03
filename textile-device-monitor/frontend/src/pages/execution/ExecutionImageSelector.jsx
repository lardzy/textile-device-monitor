import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Modal,
  Space,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleFilled,
  EyeOutlined,
  FileImageOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons';
import { executionArtifactPreviewUrl } from '../../api/execution';

const { Text } = Typography;

const MAX_IMAGES = 10;

export const imageSelectionFolderId = folder => (
  typeof folder === 'string' || typeof folder === 'number'
    ? folder
    : folder?.folder_index_entry_id
  || folder?.file_index_entry_id
  || folder?.folder_id
  || folder?.id
  || folder?.artifact_id
);

export const imageSelectionImageId = image => (
  typeof image === 'string' || typeof image === 'number'
    ? image
    : image?.file_index_entry_id
  || image?.image_id
  || image?.id
  || image?.artifact_id
  || image?.artifact?.id
);

const folderNameOf = folder => (
  folder?.folder_name
  || folder?.name
  || String(folder?.relative_path || '').split(/[\\/]/).filter(Boolean).pop()
  || '未命名文件夹'
);

const imageNameOf = image => (
  image?.name
  || image?.filename
  || image?.artifact?.filename
  || String(image?.relative_path || '').split(/[\\/]/).filter(Boolean).pop()
  || '未命名图片'
);

const imageContextOf = (image) => {
  const relativePath = String(image?.relative_path || '');
  const pathParts = relativePath.split(/[\\/]/).filter(Boolean);
  const folderName = String(image?.folder_name || '');
  const contextualParts = folderName && pathParts[0] === folderName
    ? pathParts.slice(1, -1)
    : pathParts.slice(0, -1);
  const parentPath = contextualParts.length > 0
    ? contextualParts.join(' / ')
    : '';
  return image?.folder_name
    ? [
      image.folder_name,
      image?.section_path || image?.part_name || image?.section_name || parentPath,
    ]
      .filter(Boolean)
      .join(' · ')
    : parentPath;
};

const imageSourceOf = (image) => {
  const explicit = image?.preview_url
    || image?.url
    || image?.src
    || image?.artifact?.preview_url;
  if (explicit) {
    return explicit;
  }
  const artifactId = image?.artifact_id || image?.artifact?.id;
  return artifactId ? executionArtifactPreviewUrl(artifactId) : null;
};

const folderKeysOf = folder => new Set([
  imageSelectionFolderId(folder),
  folder?.folder_id,
  folder?.folder_name,
  folder?.name,
  folder?.relative_path,
].filter(Boolean).map(String));

const imageFolderKeysOf = image => new Set([
  image?.folder_id,
  image?.top_level_folder_id,
  image?.folder_index_entry_id,
  image?.parent_id,
  image?.folder_name,
  image?.parent_name,
  image?.folder_relative_path,
  image?.parent_relative_path,
].filter(Boolean).map(String));

const imageBelongsToFolder = (image, folder) => {
  const folderKeys = folderKeysOf(folder);
  return [...imageFolderKeysOf(image)].some(key => folderKeys.has(key));
};

const uniqueById = (items, idOf) => {
  const seen = new Set();
  return items.filter((item) => {
    const id = idOf(item);
    if (!id || seen.has(String(id))) {
      return false;
    }
    seen.add(String(id));
    return true;
  });
};

const normalizePayload = (foldersValue, imagesValue) => {
  const folders = Array.isArray(foldersValue)
    ? foldersValue.filter(folder => folder && typeof folder === 'object')
    : [];
  const nestedImages = folders.flatMap(folder => (
    Array.isArray(folder.images)
      ? folder.images.map(image => ({
        ...image,
        folder_id: image.folder_id || imageSelectionFolderId(folder),
        folder_name: image.folder_name || folderNameOf(folder),
      }))
      : []
  ));
  const images = uniqueById([
    ...(Array.isArray(imagesValue) ? imagesValue : []),
    ...nestedImages,
  ].filter(image => image && typeof image === 'object'), imageSelectionImageId);
  return { folders, images };
};

function ImagePreviewModal({ images, imageId, onImageIdChange, onClose }) {
  const index = images.findIndex(image => (
    String(imageSelectionImageId(image)) === String(imageId)
  ));
  const activeImage = index >= 0 ? images[index] : null;
  const source = imageSourceOf(activeImage);

  useEffect(() => {
    if (!activeImage) {
      return undefined;
    }
    const onKeyDown = (event) => {
      if (event.key === 'ArrowLeft' && index > 0) {
        onImageIdChange(String(imageSelectionImageId(images[index - 1])));
      } else if (event.key === 'ArrowRight' && index < images.length - 1) {
        onImageIdChange(String(imageSelectionImageId(images[index + 1])));
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [activeImage, images, index, onImageIdChange]);

  return (
    <Modal
      open={Boolean(activeImage)}
      title={activeImage ? imageNameOf(activeImage) : '查看图片'}
      onCancel={onClose}
      footer={null}
      width={980}
      destroyOnHidden
      className="execution-image-preview-modal"
    >
      {activeImage && (
        <div className="execution-image-preview">
          <Button
            type="text"
            className="execution-image-preview__nav"
            icon={<LeftOutlined />}
            aria-label="上一张图片"
            disabled={index <= 0}
            onClick={() => onImageIdChange(String(imageSelectionImageId(images[index - 1])))}
          />
          <div className="execution-image-preview__stage">
            {source ? (
              <img src={source} alt={imageNameOf(activeImage)} />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="此图片暂时没有预览地址"
              />
            )}
            <Text type="secondary">{index + 1} / {images.length}</Text>
          </div>
          <Button
            type="text"
            className="execution-image-preview__nav"
            icon={<RightOutlined />}
            aria-label="下一张图片"
            disabled={index < 0 || index >= images.length - 1}
            onClick={() => onImageIdChange(String(imageSelectionImageId(images[index + 1])))}
          />
        </div>
      )}
    </Modal>
  );
}

export default function ExecutionImageSelector({
  folders: foldersValue,
  images: imagesValue,
  selectedFolderIds = [],
  selectedImageIds = [],
  onSelectedFolderIdsChange,
  onSelectedImageIdsChange,
  onPrimaryImageIdChange,
  disabled = false,
  maxImages = MAX_IMAGES,
}) {
  const [previewImageId, setPreviewImageId] = useState(null);
  const { folders, images } = useMemo(
    () => normalizePayload(foldersValue, imagesValue),
    [foldersValue, imagesValue],
  );
  const normalizedFolderIds = selectedFolderIds.map(String);
  const normalizedImageIds = selectedImageIds.map(String);
  const selectedFolderSet = new Set(normalizedFolderIds);
  const selectedImageSet = new Set(normalizedImageIds);
  const selectableFolders = folders.filter(folder => imageSelectionFolderId(folder));
  const visibleImages = folders.length === 0
    ? images
    : images.filter(image => selectableFolders.some(folder => (
      selectedFolderSet.has(String(imageSelectionFolderId(folder)))
      && imageBelongsToFolder(image, folder)
    )));

  useEffect(() => {
    if (!previewImageId) {
      return;
    }
    if (!visibleImages.some(image => (
      String(imageSelectionImageId(image)) === String(previewImageId)
    ))) {
      setPreviewImageId(null);
    }
  }, [previewImageId, visibleImages]);

  const updateImageSelection = (next) => {
    const normalized = [...new Set(next.map(String))].slice(0, maxImages);
    onSelectedImageIdsChange?.(normalized);
    onPrimaryImageIdChange?.(normalized[0] || null);
  };

  const toggleFolder = (folderId, checked) => {
    const id = String(folderId);
    const nextFolderIds = checked
      ? [...new Set([...normalizedFolderIds, id])]
      : normalizedFolderIds.filter(value => value !== id);
    onSelectedFolderIdsChange?.(nextFolderIds);

    if (!checked) {
      const folder = selectableFolders.find(item => (
        String(imageSelectionFolderId(item)) === id
      ));
      const removedImageIds = new Set(images
        .filter(image => folder && imageBelongsToFolder(image, folder))
        .map(image => String(imageSelectionImageId(image))));
      updateImageSelection(normalizedImageIds.filter(imageId => !removedImageIds.has(imageId)));
    }
  };

  const toggleImage = (imageId, checked) => {
    const id = String(imageId);
    if (checked && !selectedImageSet.has(id) && normalizedImageIds.length >= maxImages) {
      message.warning(`最多选择 ${maxImages} 张图片`);
      return;
    }
    updateImageSelection(checked
      ? [...normalizedImageIds, id]
      : normalizedImageIds.filter(value => value !== id));
  };

  const selectAllVisible = () => {
    const visibleIds = visibleImages.map(imageSelectionImageId).filter(Boolean).map(String);
    const next = [...new Set([...normalizedImageIds, ...visibleIds])];
    if (next.length > maxImages) {
      message.warning(`已按当前顺序选择前 ${maxImages} 张图片`);
    }
    updateImageSelection(next);
  };

  return (
    <div className="execution-image-selector">
      {folders.length > 0 && (
        <section className="execution-image-selector__folders">
          <div className="execution-image-selector__section-head">
            <div>
              <Text strong>1. 选择结果文件夹</Text>
              <Text type="secondary">可多选，图片会合并到下方统一选择</Text>
            </div>
            <Tag>{normalizedFolderIds.length} / {selectableFolders.length}</Tag>
          </div>
          {selectableFolders.length > 0 ? (
            <Checkbox.Group value={normalizedFolderIds}>
              {selectableFolders.map((folder) => {
                const id = String(imageSelectionFolderId(folder));
                const count = Array.isArray(folder.images)
                  ? folder.images.length
                  : images.filter(image => imageBelongsToFolder(image, folder)).length;
                return (
                  <Checkbox
                    key={id}
                    value={id}
                    disabled={disabled}
                    onChange={event => toggleFolder(id, event.target.checked)}
                  >
                    <span>
                      <strong title={folderNameOf(folder)}>{folderNameOf(folder)}</strong>
                      <small>{count} 张图片</small>
                    </span>
                  </Checkbox>
                );
              })}
            </Checkbox.Group>
          ) : (
            <Alert
              showIcon
              type="warning"
              message="匹配文件夹缺少稳定 ID，暂不能选择"
            />
          )}
        </section>
      )}

      <section className="execution-image-selector__images">
        <div className="execution-image-selector__section-head">
          <div>
            <Text strong>{folders.length > 0 ? '2. 选择结果图片' : '选择结果图片'}</Text>
            <Text type="secondary">至少 1 张，最多 {maxImages} 张；点击放大镜查看原图</Text>
          </div>
          <Tag color={normalizedImageIds.length > maxImages ? 'error' : 'blue'}>
            已选 {normalizedImageIds.length} / {maxImages}
          </Tag>
        </div>

        {folders.length > 0 && normalizedFolderIds.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="请先选择一个或多个结果文件夹"
          />
        ) : visibleImages.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="所选目录中没有可选择的图片"
          />
        ) : (
          <>
            <div className="execution-image-selector__actions">
              <Space size={6} wrap>
                <Button size="small" disabled={disabled} onClick={selectAllVisible}>
                  选择当前图片
                </Button>
                <Button
                  size="small"
                  disabled={disabled || normalizedImageIds.length === 0}
                  onClick={() => updateImageSelection([])}
                >
                  清空选择
                </Button>
              </Space>
              <Text type="secondary">共 {visibleImages.length} 张</Text>
            </div>
            <div className="execution-image-selector__grid">
              {visibleImages.map((image) => {
                const id = String(imageSelectionImageId(image));
                const selected = selectedImageSet.has(id);
                const source = imageSourceOf(image);
                return (
                  <article
                    key={id}
                    className={[
                      'execution-image-selector__image',
                      selected ? 'is-selected' : '',
                    ].filter(Boolean).join(' ')}
                  >
                    <button
                      type="button"
                      className="execution-image-selector__select"
                      disabled={disabled}
                      aria-label={`${selected ? '取消选择' : '选择'} ${imageNameOf(image)}`}
                      aria-pressed={selected}
                      onClick={() => toggleImage(id, !selected)}
                    >
                      {source ? (
                        <img src={source} alt="" loading="lazy" />
                      ) : (
                        <span className="execution-image-selector__placeholder">
                          <FileImageOutlined />
                        </span>
                      )}
                      {selected && <CheckCircleFilled className="execution-image-selector__checked" />}
                    </button>
                    <div className="execution-image-selector__image-meta">
                      <div>
                        <Tooltip title={imageNameOf(image)}>
                          <Text>{imageNameOf(image)}</Text>
                        </Tooltip>
                        {imageContextOf(image) && (
                          <small title={imageContextOf(image)}>{imageContextOf(image)}</small>
                        )}
                      </div>
                      <Button
                        type="text"
                        size="small"
                        icon={<EyeOutlined />}
                        aria-label={`查看大图 ${imageNameOf(image)}`}
                        onClick={() => setPreviewImageId(id)}
                      />
                    </div>
                  </article>
                );
              })}
            </div>
          </>
        )}
      </section>

      <ImagePreviewModal
        images={visibleImages}
        imageId={previewImageId}
        onImageIdChange={setPreviewImageId}
        onClose={() => setPreviewImageId(null)}
      />
    </div>
  );
}

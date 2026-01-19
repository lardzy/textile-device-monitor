import { Modal } from 'antd';

function ResultsModal({ open, title, url, onClose }) {
  return (
    <Modal
      title={title}
      open={open}
      onCancel={onClose}
      footer={null}
      width="90vw"
      style={{ top: 20 }}
      bodyStyle={{ height: '80vh', padding: 0 }}
      destroyOnClose
    >
      <iframe title={title} src={url} style={{ border: 'none', width: '100%', height: '100%' }} />
    </Modal>
  );
}

export default ResultsModal;

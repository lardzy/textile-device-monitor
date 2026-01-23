const normalizeUrl = (url) => {
  if (!url) {
    return '/unigraco/';
  }
  return url.endsWith('/') ? url : `${url}/`;
};

function InteractiveSegmentation() {
  const iframeUrl = normalizeUrl(import.meta.env.VITE_UNIGRACO_URL || '/unigraco/');

  return (
    <div style={{ height: 'calc(100vh - 160px)', minHeight: '640px' }}>
      <iframe
        title="UniGraCo"
        src={iframeUrl}
        style={{ width: '100%', height: '100%', border: 'none', borderRadius: '8px' }}
      />
    </div>
  );
}

export default InteractiveSegmentation;

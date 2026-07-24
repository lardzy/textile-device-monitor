export const isSameAreaImage = (left, right) => (
  Number(left) === Number(right)
);

export const isCurrentAreaRequest = (requestSeq, currentRequestSeq) => (
  requestSeq === currentRequestSeq
);

export const shouldApplyAreaImageResponse = ({
  requestSeq,
  currentRequestSeq,
  requestedImageId,
  selectedImageId,
}) => (
  isCurrentAreaRequest(requestSeq, currentRequestSeq)
  && isSameAreaImage(requestedImageId, selectedImageId)
);

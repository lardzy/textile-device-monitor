import os
from pathlib import Path

import gradio as gr
import numpy as np
import torch

from isegm.inference import utils as infer_utils
from isegm.utils import exp
from web_controller import WebInteractiveController

torch.set_grad_enabled(False)


def normalize_image(image):
    if image is None:
        return None
    image = np.asarray(image)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    if image.ndim == 3 and image.shape[-1] == 4:
        image = image[:, :, :3]
    if image.dtype != np.uint8:
        if image.max() <= 1.0:
            image = image * 255
        image = np.clip(image, 0, 255).astype(np.uint8)
    return image


def resolve_weights_path(config_path):
    cfg = exp.load_config_file(config_path, return_edict=True)
    weights_dir = Path(config_path).parent / cfg.INTERACTIVE_MODELS_PATH
    return cfg, weights_dir.resolve()


def resolve_checkpoint_path(weights_dir, checkpoint_name):
    if Path(checkpoint_name).is_absolute():
        return checkpoint_name
    return infer_utils.find_checkpoint(weights_dir, checkpoint_name)


def resolve_optional_path(weights_dir, path_value):
    if not path_value:
        return None
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = weights_dir / candidate
    return str(candidate)


def load_model():
    config_path = Path(os.getenv("UNIGRACO_CONFIG", "/opt/unigraco/config.yml"))
    _, weights_dir = resolve_weights_path(config_path)
    checkpoint_name = os.getenv("UNIGRACO_CHECKPOINT", "sbd_vit_base.pth")
    lora_checkpoint = os.getenv("UNIGRACO_LORA_CHECKPOINT", "GraCo_base_lora.pth")
    device_name = os.getenv("UNIGRACO_DEVICE", "cuda:0")

    if device_name.startswith("cuda") and not torch.cuda.is_available():
        device_name = "cpu"

    device = torch.device(device_name)
    checkpoint_path = resolve_checkpoint_path(weights_dir, checkpoint_name)
    lora_path = resolve_optional_path(weights_dir, lora_checkpoint)

    if not Path(checkpoint_path).exists():
        raise FileNotFoundError(f"Model checkpoint not found: {checkpoint_path}")
    if lora_path is not None and not Path(lora_path).exists():
        raise FileNotFoundError(f"LoRA checkpoint not found: {lora_path}")

    model = infer_utils.load_is_model(
        checkpoint_path,
        device,
        eval_ritm=False,
        lora_checkpoint=lora_path,
        cpu_dist_maps=True,
    )

    return model, device


def build_predictor_params(prob_thresh, max_size):
    return {
        "brs_mode": "NoBRS",
        "prob_thresh": prob_thresh,
        "with_flip": False,
        "zoom_in_params": None,
        "predictor_params": {"net_clicks_limit": None, "max_size": max_size},
        "brs_opt_func_params": {"min_iou_diff": 1e-3},
        "lbfgs_params": {"maxfun": 20},
    }


MODEL, DEVICE = load_model()
MAX_SIZE = int(os.getenv("UNIGRACO_MAX_SIZE", "800"))


def create_session(image, granularity, prob_thresh, alpha_blend, click_radius):
    image = normalize_image(image)
    if image is None:
        return None, None

    predictor_params = build_predictor_params(prob_thresh, MAX_SIZE)
    controller = WebInteractiveController(
        MODEL,
        DEVICE,
        predictor_params,
        prob_thresh=prob_thresh,
        granularity=granularity,
    )
    controller.set_image(image)
    visualization = controller.get_visualization(alpha_blend, click_radius)
    return visualization, controller


def handle_click(
    state,
    click_type,
    granularity,
    prob_thresh,
    alpha_blend,
    click_radius,
    evt: gr.SelectData,
):
    if state is None:
        return None, state
    state.granularity = granularity
    state.prob_thresh = prob_thresh
    x, y = evt.index
    is_positive = click_type == "Positive"
    state.add_click(int(x), int(y), is_positive)
    visualization = state.get_visualization(alpha_blend, click_radius)
    return visualization, state


def undo_click(state, alpha_blend, click_radius):
    if state is None:
        return None, state
    state.undo_click()
    visualization = state.get_visualization(alpha_blend, click_radius)
    return visualization, state


def reset_clicks(state, alpha_blend, click_radius):
    if state is None:
        return None, state
    state.reset_last_object(update_image=False)
    visualization = state.get_visualization(alpha_blend, click_radius)
    return visualization, state


def finish_object(state, alpha_blend, click_radius):
    if state is None:
        return None, state
    state.finish_object()
    visualization = state.get_visualization(alpha_blend, click_radius)
    return visualization, state


def clear_image():
    return None, None, None


def refresh_view(state, prob_thresh, alpha_blend, click_radius):
    if state is None:
        return None, state
    state.prob_thresh = prob_thresh
    visualization = state.get_visualization(alpha_blend, click_radius)
    return visualization, state


def build_demo():
    with gr.Blocks(title="UniGraCo Interactive Segmentation") as demo:
        gr.Markdown("## UniGraCo Interactive Segmentation")

        with gr.Row():
            input_image = gr.Image(type="numpy", label="Input Image")
            output_image = gr.Image(
                type="numpy", label="Segmentation", interactive=True
            )

        with gr.Row():
            click_type = gr.Radio(
                choices=["Positive", "Negative"], value="Positive", label="Click Type"
            )
            granularity = gr.Slider(0.0, 1.0, value=1.0, step=0.01, label="Granularity")
            prob_thresh = gr.Slider(0.0, 1.0, value=0.5, step=0.01, label="Threshold")
            alpha_blend = gr.Slider(
                0.0, 1.0, value=0.5, step=0.01, label="Overlay Alpha"
            )
            click_radius = gr.Slider(1, 10, value=3, step=1, label="Click Radius")

        with gr.Row():
            undo_button = gr.Button("Undo Click")
            reset_button = gr.Button("Reset Clicks")
            finish_button = gr.Button("Finish Object")
            clear_button = gr.Button("Clear Image")

        state = gr.State(None)

        input_image.change(
            create_session,
            inputs=[input_image, granularity, prob_thresh, alpha_blend, click_radius],
            outputs=[output_image, state],
        )
        output_image.select(
            handle_click,
            inputs=[
                state,
                click_type,
                granularity,
                prob_thresh,
                alpha_blend,
                click_radius,
            ],
            outputs=[output_image, state],
        )
        undo_button.click(
            undo_click,
            inputs=[state, alpha_blend, click_radius],
            outputs=[output_image, state],
        )
        reset_button.click(
            reset_clicks,
            inputs=[state, alpha_blend, click_radius],
            outputs=[output_image, state],
        )
        finish_button.click(
            finish_object,
            inputs=[state, alpha_blend, click_radius],
            outputs=[output_image, state],
        )
        clear_button.click(clear_image, outputs=[input_image, output_image, state])

        prob_thresh.change(
            refresh_view,
            inputs=[state, prob_thresh, alpha_blend, click_radius],
            outputs=[output_image, state],
        )
        alpha_blend.change(
            refresh_view,
            inputs=[state, prob_thresh, alpha_blend, click_radius],
            outputs=[output_image, state],
        )
        click_radius.change(
            refresh_view,
            inputs=[state, prob_thresh, alpha_blend, click_radius],
            outputs=[output_image, state],
        )

    return demo


if __name__ == "__main__":
    max_concurrency = int(os.getenv("UNIGRACO_MAX_CONCURRENCY", "2"))
    max_queue = int(os.getenv("UNIGRACO_MAX_QUEUE", "10"))
    root_path = os.getenv("UNIGRACO_ROOT_PATH", "").strip()
    if root_path in ("", "/"):
        root_path = None

    app = build_demo()
    app.queue(default_concurrency_limit=max_concurrency, max_size=max_queue)
    app.launch(server_name="0.0.0.0", server_port=7860, root_path=root_path)

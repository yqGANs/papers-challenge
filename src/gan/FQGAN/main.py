import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchvision.utils import make_grid
from apex import amp

from config import Config
from datasets import data_utils
from training.logger import Logger
from training.utils import set_seed, count_parameters_float32
from models.FQGAN_64 import Generator, Discriminator
from models.losses import ortho_reg, ProbLoss


def setup(cfg, logger):
    if cfg is not None:
        logger.print_log(f"configuration: {cfg}")

    if cfg.seed:
        set_seed(cfg.seed)
        logger.print_log(f"seed setting: {cfg.seed}")

    logger.print_log(f"device setting: {cfg.device}")


def build_dataset(cfg, logger):
    dataset = data_utils.get_celeba_dataset(
        root=cfg.datapath, image_size=cfg.image_size
    )
    logger.print_log(len(dataset))

    dataloader = DataLoader(
        dataset, batch_size=cfg.batch_size, num_workers=cfg.num_workers, shuffle=True
    )

    real_batch = next(iter(dataloader))
    logger.print_log(f"sample real batch size: {real_batch[0].size()}")
    logger.add_image("sample", real_batch[0])

    return dataset, dataloader


def build_model(cfg, logger):
    netG = Generator(cfg.nz, cfg.ngf, cfg.nc, cfg.bottom_width, cfg.use_sn).to(
        cfg.device
    )
    logger.print_log(f"Generator Memory: {count_parameters_float32(netG):.2f} MB")

    netD = Discriminator(
        cfg.nc, cfg.ndf, cfg.use_sn, cfg.vq_type, cfg.dict_size, cfg.quant_layers
    ).to(cfg.device)
    logger.print_log(f"Discriminator Memory: {count_parameters_float32(netD):.2f} MB")

    if (cfg.is_cuda) and (cfg.ngpu > 1):
        netG = nn.DataParallel(netG, list(range(cfg.ngpu)))
        netD = nn.DataParallel(netD, list(range(cfg.ngpu)))

    # Setup Adam optimizers for both G and D
    optimizerD = optim.Adam(netD.parameters(), lr=cfg.lrD, betas=cfg.betas)
    optimizerG = optim.Adam(netG.parameters(), lr=cfg.lrG, betas=cfg.betas)

    if cfg.amp:
        netG, optimizerG = amp.initialize(netG, optimizerG, opt_level=cfg.opt_level)
        netD, optimizerD = amp.initialize(netD, optimizerD, opt_level=cfg.opt_level)

    return netG, netD, optimizerD, optimizerG


def main(cfg):

    logger = Logger(cfg.log_dir, cfg.flush_secs)
    setup(cfg, logger)

    dataset, dataloader = build_dataset(cfg, logger)

    netG, netD, optimizerG, optimizerD = build_model(cfg, logger)

    # Initialize BCELoss function
    criterion = ProbLoss(cfg.loss_type, cfg.batch_size, cfg.device)

    # Create batch of latent vectors that we will use to visualize
    #  the progression of the generator
    fixed_noise = torch.randn(64, cfg.nz, device=cfg.device)
    logger.print_log(f"fixed noise shape: {fixed_noise.size()}")

    history = {"img_list": [], "g_losses": [], "d_losses": []}
    iters = 0

    logger.print_log("Starting Training Loop...")

    # For each epoch
    for epoch in range(cfg.n_epochs):
        netG.train()
        netD.train()
        # For each batch in the dataloader
        for i, (real_imgs, labels) in enumerate(dataloader, 0):

            # send real imgs to cuda
            real_imgs = real_imgs.to(cfg.device)
            bs = real_imgs.size(0)

            ############################
            # (1) Update D network: maximize log(D(x)) + log(1 - D(G(z)))
            ###########################
            # Train with all-real batch
            netD.zero_grad()
            # Forward pass real batch through D
            if cfg.vq_type:
                output_real, quant_loss_real, embed_idx_real = netD(real_imgs)
            else:
                output_real = netD(real_imgs)
            # Calculate loss on all-real batch
            lossD_real = criterion(output_real, "dis_real")
            if cfg.vq_type:
                lossD_real += quant_loss_real

            # Train with all-fake batch
            fake = netG.generate_images(bs, cfg.device)
            # Classify all fake batch with D
            if cfg.vq_type:
                output_fake, quant_loss_fake, embed_idx_fake = netD(fake.detach())
            else:
                output_fake = netD(fake.detach())
            # Calculate D's loss on the all-fake batch
            lossD_fake = criterion(output_fake, "dis_fake")
            if cfg.vq_type:
                lossD_fake += quant_loss_fake

            # Add the gradients from the all-real and all-fake batches
            lossD = lossD_real + lossD_fake
            if cfg.amp:
                with amp.scale_loss(lossD, optimizerD) as scaled_loss:
                    scaled_loss.backward(retain_graph=True)
            else:
                lossD.backward(retain_graph=True)

            # apply Orthogonal Regularization
            if cfg.is_ortho:
                ortho_reg(netD, cfg.is_ortho)

            # Update D
            optimizerD.step()

            ############################
            # (2) Update G network: maximize log(D(G(z)))
            ###########################
            netG.zero_grad()
            # Since we just updated D, perform another forward pass of all-fake batch through D
            if cfg.vq_type:
                output, quant_loss_G, _ = netD(fake)
            else:
                output = netD(fake)
            # Calculate G's loss based on this output
            lossG = criterion(output, "gen")
            if cfg.vq_type:
                lossG += quant_loss_G

            # Calculate gradients for G
            with torch.autograd.detect_anomaly():
                if cfg.amp:
                    with amp.scale_loss(lossG, optimizerG) as scaled_loss:
                        scaled_loss.backward()
                else:
                    lossG.backward()

            # apply Orthogonal Regularization
            if cfg.is_ortho:
                ortho_reg(netG, cfg.is_ortho)

            # # Update G
            optimizerG.step()

            # Output training stats
            if i % cfg.print_iters == 0:
                D_x  = netD.compute_probs(output_real)
                D_Gz = netD.compute_probs(output_fake)

                msg = "[{:3d}/{:3d}][{:5d}/{:5d}] ".format(
                    epoch, cfg.n_epochs, i, len(dataloader)
                )
                msg += "Loss_D: {:.4f} Loss_G: {:.4f} ".format(
                    lossD.item(), lossG.item()
                )
                msg += "D(x): {:.4f} D(G(z)): {:.4f}".format(D_x, D_Gz)

                logger.print_log(msg)

                # logger.add_histogram("g_layer1", netG.layer1[0].weight.data, global_step=iters)
                # logger.add_histogram("g_layer2", netG.layer2[0].weight.data, global_step=iters)
                # logger.add_histogram("g_layer3", netG.layer3[0].weight.data, global_step=iters)
                # logger.add_histogram("g_layer4", netG.layer4[0].weight.data, global_step=iters)
                # logger.add_histogram("g_layer5", netG.last_conv.weight.data, global_step=iters)

                # logger.add_histogram("d_layer1", netD.layer1[0].weight.data, global_step=iters)
                # logger.add_histogram("d_layer2", netD.layer2[0].weight.data, global_step=iters)
                # logger.add_histogram("d_layer3", netD.layer3[0].weight.data, global_step=iters)
                # logger.add_histogram("d_layer4", netD.layer4[0].weight.data, global_step=iters)

                # if cfg.use_vq:
                #     logger.add_embedding(
                #         "d_vq2_embedding", netD.vq2.embed.data, global_step=iters
                #     )
                #     logger.add_embedding(
                #         "d_vq3_embedding", netD.vq3.embed.data, global_step=iters
                #     )
                #     logger.add_embedding(
                #         "d_vq4_embedding", netD.vq4.embed.data, global_step=iters
                #     )

                logger.add_scalar("g_losses", lossG.item(), global_step=iters)
                logger.add_scalar("d_losses", lossD.item(), global_step=iters)
                # if cfg.is_ortho:
                #     logger.add_scalar(
                #         "ortho_losses", ortho_loss.item(), global_step=iters
                #     )

                logger.add_scalar("D(x)", D_x, global_step=iters)
                logger.add_scalar("D(G(z))", D_Gz, global_step=iters)

            # Save Losses for plotting later
            history["g_losses"].append(lossG.item())
            history["d_losses"].append(lossD.item())

            iters += 1

        # Check how the generator is doing by saving G's output on fixed_noise
        netG.eval()
        netD.eval()
        with torch.no_grad():
            fake = netG(fixed_noise).detach().cpu()

        history["img_list"].append(make_grid(fake, padding=2, normalize=True))
        # output to tensorboard
        logger.add_image("img", fake, global_step=epoch)

        # do checkpointing
        torch.save(netG.state_dict(), "%s/netG_epoch_%d.pth" % (cfg.output, epoch))
        torch.save(netD.state_dict(), "%s/netD_epoch_%d.pth" % (cfg.output, epoch))

    logger.close_writers()


if __name__ == "__main__":
    cfg = Config()
    print(type(cfg.vq_type))
    main(cfg)

import torch
from torch.utils.tensorboard import SummaryWriter
import numpy as np
from tqdm import tqdm
from utils.utils import printlog
from abc import abstractmethod
import os
from experiments.configs.base_expconfig import Base_ExpConfig

class BaseModelClass():
    def __init__(
        self,
        config: Base_ExpConfig,
        train_data=None,
        val_data=None,
    ): 
        self.train_data = train_data
        self.val_data = val_data
        self.model_type = config.model_type

        self.run_dir = os.path.join("experiments/out", config.data_name, config.run_dir)
        os.makedirs(self.run_dir, exist_ok=True)
        self.device = config.device
        self.batch_size = config.batch_size
        self.epochs = config.epochs
        self.save_epochfreq = config.save_epochfreq
        self.encoder_type = config.encoder_type

        if config.encoder_type == 'TS2Vec':        
            # this encoder architecture is shared across all benchmarks
            self._encoder = TSEncoder(input_dims=config.input_dims, output_dims=320, hidden_dims=64, depth=10).to(self.device)
        elif config.encoder_type == 'RNN':
            # Replace TSEncoder with RnnEncoder
            self._encoder = RnnEncoder(hidden_size=100, in_channel=config.input_dims, encoding_size=320, cell_type='GRU', num_layers=1, device=self.device).to(self.device)
        self.optimizer = torch.optim.AdamW(self._encoder.parameters(), lr=config.lr)
        self.encoder = torch.optim.swa_utils.AveragedModel(self._encoder)
        self.encoder.update_parameters(self._encoder)
        self.adf = config.adf
        self.w = config.w

    
    @abstractmethod
    def setup_dataloader(self, data: np.array, train: bool) -> torch.utils.data.DataLoader:
        ...

    @abstractmethod
    def run_one_epoch(self, dataloader: torch.utils.data.DataLoader, train: bool):
        ...

    def fit(self):
        if self.adf == False:
            method_neighbours = 'sim'
        else:
            method_neighbours = 'adf'
        printlog(f"Begin Training {self.model_type} SSL with {self.encoder_type}-{method_neighbours} with W= {self.w}", self.run_dir)

        writer = SummaryWriter(log_dir=os.path.join(self.run_dir, "tb"))

        train_loader, val_loader = self.setup_dataloader(self.train_data, train=True), self.setup_dataloader(self.val_data, train=False)        
        train_loss_list, val_loss_list = [], []
        best_val_loss = np.inf
        for epoch in tqdm(range(self.epochs), desc=f"{self.model_type} SSL Encoder Fitting Progress"):
            
            train_loss = self.run_one_epoch(train_loader, train=True)
            train_loss_list.append(train_loss)

            val_loss = self.run_one_epoch(val_loader, train=False)
            val_loss_list.append(val_loss)
            
            state_dict = {
                "encoder": self.encoder.state_dict(), # averaged model
                "_encoder": self._encoder.state_dict(),
                "optimizer": self.optimizer.state_dict(),
                "epoch": epoch,
            }
            if epoch % self.save_epochfreq == 0:
                torch.save(state_dict, f'{self.run_dir}/checkpoint_epoch{epoch}.pkl')
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                torch.save(state_dict, f'{self.run_dir}/checkpoint_best.pkl')
            torch.save(state_dict, f'{self.run_dir}/checkpoint_latest.pkl')

            printlog(f"Epoch #{epoch}: train loss={train_loss}, val loss={val_loss}", self.run_dir)
            
            writer.add_scalar('Contrastive Loss/Train', train_loss, epoch)
            writer.add_scalar('Contrastive Loss/Val', val_loss, epoch)

    # encode for ts2vec
    def encode(self, data: np.array):
        
        dataset = torch.utils.data.TensorDataset(torch.from_numpy(data).to(torch.float))
        loader = torch.utils.data.DataLoader(dataset, batch_size=self.batch_size, num_workers=torch.get_num_threads())

        self.encoder.eval()
        with torch.no_grad():
            output = []
            for batch in tqdm(loader, leave=False):
                x = batch[0]
                # print("shape of x before encoding",x.shape)
                out = self.encoder(x.to(self.device, non_blocking=True))
                # print("shape of x after encoding",out.shape)
                if self.encoder_type == 'TS2Vec':
                    out = torch.nn.functional.max_pool1d(out.transpose(1, 2), kernel_size = out.size(1)).transpose(1, 2).squeeze(1)
                    # print("shape of x after encoding and max pooling",out.shape)
                output.append(out)
            output = torch.cat(output, dim=0)
        self.encoder.train()

        return output.detach().cpu().numpy()

    
    def load(self, ckpt="best"):
        state_dict = torch.load(f'{self.run_dir}/checkpoint_{ckpt}.pkl', map_location=self.device)

        print(self.encoder.load_state_dict(state_dict["encoder"]))
        printlog(f"Reloading {self.model_type} Encoder's ckpt {ckpt}, which is from epoch {state_dict['epoch']}", self.run_dir)
    



class TSEncoder(torch.nn.Module):
    def __init__(self, input_dims, output_dims, hidden_dims=64, depth=10):
        super().__init__()
        self.input_dims = input_dims
        self.output_dims = output_dims
        self.hidden_dims = hidden_dims
        self.input_fc = torch.nn.Linear(input_dims, hidden_dims)
        self.feature_extractor = DilatedConvEncoder(
            hidden_dims,
            [hidden_dims] * depth + [output_dims],
            kernel_size=3
        )
        self.repr_dropout = torch.nn.Dropout(p=0.1)
        
    def forward(self, x, mask=None):  # x: B x T x input_dims
        nan_mask = ~x.isnan().any(axis=-1) # this is necessary  bc TS2vec purposely introduces nans that we need to 0 out
        x[~nan_mask] = 0

        x = self.input_fc(x)  # B x T x Ch
        
        if mask == 'binomial':
            mask = torch.from_numpy(np.random.binomial(1, 0.5, size=(x.size(0),  x.size(1)))).to(x.device)
            mask &= nan_mask
            x[~mask] = 0
        
        # conv encoder
        x = x.transpose(1, 2)  # B x Ch x T
        # print("shape of x before feature extractor",x.shape)
        x = self.repr_dropout(self.feature_extractor(x))  # B x Co x T
        # print("shape of x after feature extractor",x.shape)
        x = x.transpose(1, 2)  # B x T x Co
        
        return x
    
class DilatedConvEncoder(torch.nn.Module):
    def __init__(self, in_channels, channels, kernel_size):
        super().__init__()
        self.net = torch.nn.Sequential(*[
            ConvBlock(
                channels[i-1] if i > 0 else in_channels,
                channels[i],
                kernel_size=kernel_size,
                dilation=2**i,
                final=(i == len(channels)-1)
            )
            for i in range(len(channels))
        ])
        
    def forward(self, x):
        return self.net(x)

class ConvBlock(torch.nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, dilation, final=False):
        super().__init__()
        self.conv1 = SamePadConv(in_channels, out_channels, kernel_size, dilation=dilation)
        self.conv2 = SamePadConv(out_channels, out_channels, kernel_size, dilation=dilation)
        self.projector = torch.nn.Conv1d(in_channels, out_channels, 1) if in_channels != out_channels or final else None
    
    def forward(self, x):
        residual = x if self.projector is None else self.projector(x)
        x = torch.nn.functional.gelu(x)
        x = self.conv1(x)
        x = torch.nn.functional.gelu(x)
        x = self.conv2(x)
        return x + residual

class SamePadConv(torch.nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, dilation=1, groups=1):
        super().__init__()
        self.receptive_field = (kernel_size - 1) * dilation + 1
        padding = self.receptive_field // 2
        self.conv = torch.nn.Conv1d(
            in_channels, out_channels, kernel_size,
            padding=padding,
            dilation=dilation,
            groups=groups
        )
        self.remove = 1 if self.receptive_field % 2 == 0 else 0
        
    def forward(self, x):
        out = self.conv(x)
        if self.remove > 0:
            out = out[:, :, : -self.remove]
        return out
    
class RnnEncoder(torch.nn.Module):
    def __init__(self, hidden_size, in_channel, encoding_size, cell_type='GRU', num_layers=1, device='cpu', dropout=0, bidirectional=True):
        super(RnnEncoder, self).__init__()
        self.hidden_size = hidden_size
        self.in_channel = in_channel
        self.num_layers = num_layers
        self.cell_type = cell_type
        self.encoding_size = encoding_size
        self.bidirectional = bidirectional
        self.device = device

        self.nn = torch.nn.Sequential(torch.nn.Linear(self.hidden_size*(int(self.bidirectional) + 1), self.encoding_size)).to(device)
        if cell_type=='GRU':
            self.rnn = torch.nn.GRU(input_size=in_channel, hidden_size=hidden_size, num_layers=num_layers,
                                    batch_first=False, dropout=dropout, bidirectional=bidirectional).to(device)

        elif cell_type=='LSTM':
            self.rnn = torch.nn.LSTM(input_size=in_channel, hidden_size=hidden_size, num_layers=num_layers,
                                    batch_first=False, dropout=dropout, bidirectional=bidirectional).to(device)
        else:
            raise ValueError('Cell type not defined, must be one of the following {GRU, LSTM, RNN}')

    def forward(self, x):
        x = x.permute(1,0,2)
        if self.cell_type=='GRU':
            past = torch.zeros(self.num_layers * (int(self.bidirectional) + 1), x.shape[1], self.hidden_size).to(self.device)
        elif self.cell_type=='LSTM':
            h_0 = torch.zeros(self.num_layers * (int(self.bidirectional) + 1), x.shape[1], self.hidden_size).to(self.device)
            c_0 = torch.zeros(self.num_layers * (int(self.bidirectional) + 1), x.shape[1], self.hidden_size).to(self.device)
            past = (h_0, c_0)
        # print(f"Input tensor shape before passing to RNN batch size, windows, channels\n: {x.shape}")  # Print the shape of x
        out, _ = self.rnn(x.to(self.device), past)
        # print(f"Input tensor shape after passing to RNN batch size, enconding size:\n {out.shape}") 
        encodings = self.nn(out[-1].squeeze(0))  # Process the output of the RNN
        return encodings


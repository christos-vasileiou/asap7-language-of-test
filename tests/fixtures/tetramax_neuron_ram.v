module neuron_ram ( clock, weights_in, write_enable, address, weight_valid,
read_enable, weights_out, loaded );
input [15:0] weights_in;
input [9:0] address;
output [15:0] weights_out;
input clock, write_enable, weight_valid, read_enable;
output loaded;
wire   \weights_out[15] ;
assign weights_out[0] = \weights_out[15] ;
assign weights_out[1] = \weights_out[15] ;
assign weights_out[2] = \weights_out[15] ;
assign weights_out[3] = \weights_out[15] ;
assign weights_out[4] = \weights_out[15] ;
assign weights_out[5] = \weights_out[15] ;
assign weights_out[6] = \weights_out[15] ;
assign weights_out[7] = \weights_out[15] ;
assign weights_out[8] = \weights_out[15] ;
assign weights_out[9] = \weights_out[15] ;
assign weights_out[10] = \weights_out[15] ;
assign weights_out[11] = \weights_out[15] ;
assign weights_out[12] = \weights_out[15] ;
assign weights_out[13] = \weights_out[15] ;
assign weights_out[14] = \weights_out[15] ;
assign weights_out[15] = \weights_out[15] ;
TIELOx1_ASAP7_75t_R U5 ( .L(\weights_out[15] ) );
TIEHIx1_ASAP7_75t_R U6 ( .H(loaded) );
endmodule

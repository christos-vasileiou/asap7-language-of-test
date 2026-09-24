module gray_decode ( gray, gray_decode );
input [7:0] gray;
output [7:0] gray_decode;
assign gray_decode[7] = gray[7];
XOR2xp5_ASAP7_75t_R U8 ( .A(gray_decode[7]), .B(gray[6]), .Y(gray_decode[6])
);
XOR2xp5_ASAP7_75t_R U9 ( .A(gray_decode[6]), .B(gray[5]), .Y(gray_decode[5])
);
XOR2xp5_ASAP7_75t_R U10 ( .A(gray_decode[5]), .B(gray[4]), .Y(gray_decode[4]) );
XOR2xp5_ASAP7_75t_R U11 ( .A(gray_decode[4]), .B(gray[3]), .Y(gray_decode[3]) );
XOR2xp5_ASAP7_75t_R U12 ( .A(gray_decode[3]), .B(gray[2]), .Y(gray_decode[2]) );
XOR2xp5_ASAP7_75t_R U13 ( .A(gray_decode[2]), .B(gray[1]), .Y(gray_decode[1]) );
XOR2xp5_ASAP7_75t_R U14 ( .A(gray[0]), .B(gray_decode[1]), .Y(gray_decode[0]) );
endmodule